"""Download videos and trim segments with ffmpeg."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from dedupe import classify_url
from engine import (
    DownloadCancelled,
    _YtdlpLogger,
    build_ydl_options,
    ensure_ytdlp_on_path,
    find_ffmpeg_executable,
    find_ffprobe_executable,
    inspect_environment,
    ytdlp_is_ready,
)

ProgressCallback = Callable[[str], None]
StatusCallback = Callable[[float, str], None]


def _subprocess_hide_window_kwargs() -> dict[str, Any]:
    """Prevent ffmpeg/ffprobe console windows on Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


@contextmanager
def _hide_console_subprocess() -> Iterator[None]:
    """Hide child consoles (yt-dlp merge/ffmpeg) while the block runs."""
    if sys.platform != "win32":
        yield
        return
    original_popen = subprocess.Popen
    hide_kwargs = _subprocess_hide_window_kwargs()

    def popen_hidden(*args: Any, **kwargs: Any) -> subprocess.Popen:
        kwargs.setdefault("creationflags", hide_kwargs.get("creationflags", 0))
        kwargs.setdefault("startupinfo", hide_kwargs.get("startupinfo"))
        return original_popen(*args, **kwargs)

    subprocess.Popen = popen_hidden  # type: ignore[assignment]
    try:
        yield
    finally:
        subprocess.Popen = original_popen


@dataclass(frozen=True)
class ClipSegment:
    start: float
    end: float


@dataclass(frozen=True)
class ClipUrlJob:
    url: str
    segments: list[ClipSegment]


@dataclass(frozen=True)
class ClipLocalJob:
    source_path: Path
    segments: list[ClipSegment]


LOCAL_VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".ts", ".wmv", ".mpeg", ".mpg"}
)


def is_local_video_file(path: Path) -> bool:
    return path.suffix.lower() in LOCAL_VIDEO_EXTENSIONS


def seconds_from_hms(hours: str, minutes: str, seconds: str) -> float:
    """Build duration in seconds from hour/minute/second fields."""

    def part(value: str, name: str, max_val: Optional[int] = None) -> int:
        text = value.strip()
        if not text:
            return 0
        if not text.isdigit():
            raise ValueError(name)
        num = int(text)
        if num < 0:
            raise ValueError(name)
        if max_val is not None and num > max_val:
            raise ValueError(name)
        return num

    h = part(hours, "hours")
    m = part(minutes, "minutes", 59)
    s = part(seconds, "seconds", 59)
    return float(h * 3600 + m * 60 + s)


def parse_timecode(value: str) -> float:
    """Parse seconds or HH:MM:SS / MM:SS timecodes."""
    text = value.strip()
    if not text:
        raise ValueError("empty time")

    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)

    if ":" not in text:
        raise ValueError(f"invalid time: {value}")

    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"invalid time: {value}")

    try:
        nums = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"invalid time: {value}") from exc

    if any(n < 0 for n in nums):
        raise ValueError(f"invalid time: {value}")

    total = 0.0
    for n in nums:
        total = total * 60.0 + n
    return total


def format_timecode(seconds: float) -> str:
    whole = int(seconds)
    frac = seconds - whole
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        base = f"{h:02d}:{m:02d}:{s:02d}"
    else:
        base = f"{m:02d}:{s:02d}"
    if frac:
        return f"{base}.{frac:.3f}".rstrip("0").rstrip(".")
    return base


def _sanitize_filename(name: str, max_len: int = 80) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    if not cleaned:
        cleaned = "clip"
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned


_SKIP_SUFFIXES = {".part", ".ytdl", ".tmp", ".temp"}


def _is_complete_download(path: Path) -> bool:
    if not path.is_file():
        return False
    name = path.name.lower()
    if any(name.endswith(s) for s in _SKIP_SUFFIXES):
        return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _find_downloaded_file(temp_dir: Path, video_id: str) -> Optional[Path]:
    if not temp_dir.is_dir():
        return None

    for ext in ("mp4", "mkv", "webm", "m4a", "mov"):
        direct = temp_dir / f"{video_id}.{ext}"
        if _is_complete_download(direct):
            return direct

    candidates: list[Path] = []
    prefix = video_id.lower()
    for path in temp_dir.iterdir():
        if not _is_complete_download(path):
            continue
        if not path.name.lower().startswith(prefix):
            continue
        candidates.append(path)

    if not candidates:
        return None

    def sort_key(p: Path) -> tuple[int, int]:
        ext_rank = {".mp4": 0, ".mkv": 1, ".webm": 2, ".mov": 3}.get(
            p.suffix.lower(), 9
        )
        merged = 0 if p.stem == video_id else 1
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return (merged, ext_rank, -size)

    return min(candidates, key=sort_key)


def _resolve_downloaded_file(
    temp_dir: Path,
    video_id: str,
    info: dict,
    ydl: Any | None = None,
) -> Optional[Path]:
    """Locate the merged/local file after yt-dlp finishes."""
    found = _find_downloaded_file(temp_dir, video_id)
    if found:
        return found

    for entry in info.get("requested_downloads") or []:
        filepath = entry.get("filepath")
        if filepath:
            path = Path(filepath)
            if _is_complete_download(path):
                return path

    if ydl is not None:
        for ext in ("mp4", "mkv", "webm"):
            try:
                path = Path(ydl.prepare_filename(info, ext=ext))
            except Exception:
                continue
            if _is_complete_download(path):
                return path

    for _ in range(20):
        found = _find_downloaded_file(temp_dir, video_id)
        if found:
            return found
        time.sleep(0.5)
    return None


def _log_cache_dir_listing(
    temp_dir: Path, video_id: str, log: Optional[ProgressCallback]
) -> None:
    if log is None or not temp_dir.is_dir():
        return
    names = sorted(p.name for p in temp_dir.iterdir())
    if not names:
        log(f"缓存目录为空: {temp_dir}")
        return
    log(f"缓存目录 {temp_dir} 中与 {video_id} 相关的文件:")
    for name in names:
        if name.lower().startswith(video_id.lower()):
            log(f"  - {name}")
    if not any(n.lower().startswith(video_id.lower()) for n in names):
        log(f"  （无匹配项；目录共 {len(names)} 个文件）")


def _mp4_header_valid(path: Path) -> bool:
    try:
        if path.stat().st_size < 4096:
            return False
        with path.open("rb") as handle:
            header = handle.read(32)
        return len(header) >= 12 and header[4:8] == b"ftyp"
    except OSError:
        return False


def probe_media_duration_ffprobe(
    path: Path, ffprobe_exe: Path
) -> Optional[float]:
    cmd = [
        str(ffprobe_exe),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        **_subprocess_hide_window_kwargs(),
    )
    text = (result.stdout or result.stderr or "").strip()
    if not text:
        return None
    try:
        value = float(text.splitlines()[0])
    except ValueError:
        return None
    return value if value > 0 else None


def probe_media_duration(path: Path, ffmpeg_exe: Path) -> Optional[float]:
    """Read media duration in seconds via ffmpeg -i (fallback when ffprobe missing)."""
    cmd = [
        str(ffmpeg_exe),
        "-hide_banner",
        "-i",
        str(path),
    ]
    result = _run_ffmpeg(cmd)
    text = (result.stderr or "") + (result.stdout or "")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_source_duration(
    path: Path,
    *,
    ffmpeg_exe: Path,
    ffprobe_exe: Optional[Path] = None,
) -> Optional[float]:
    if ffprobe_exe is not None:
        duration = probe_media_duration_ffprobe(path, ffprobe_exe)
        if duration is not None:
            return duration
    return probe_media_duration(path, ffmpeg_exe)


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg/ffprobe without PIPE deadlock on long jobs."""
    log_path = Path(tempfile.gettempdir()) / f"yd_ffmpeg_{uuid.uuid4().hex}.log"
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as err_file:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err_file,
                check=False,
                **_subprocess_hide_window_kwargs(),
            )
        stderr = log_path.read_text(encoding="utf-8", errors="replace")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=result.returncode,
            stdout="",
            stderr=stderr,
        )
    finally:
        log_path.unlink(missing_ok=True)


def _ffmpeg_error(result: subprocess.CompletedProcess[str]) -> str:
    stderr = (result.stderr or result.stdout or "ffmpeg failed").strip()
    return stderr[-800:] if len(stderr) > 800 else stderr


def _output_looks_usable(
    path: Path,
    expected_duration: float,
    ffmpeg_exe: Path,
) -> tuple[bool, str, Optional[float]]:
    if not path.is_file():
        return False, "output file missing", None

    size = path.stat().st_size
    min_bytes = int(max(80_000, expected_duration * 4_000))
    if size < min_bytes:
        return False, f"file too small ({size} bytes)", None

    if not _mp4_header_valid(path):
        return False, "invalid MP4 header", None

    out_duration = probe_media_duration(path, ffmpeg_exe)
    if out_duration is not None:
        # Reject clips that run past the requested end (common with stream copy / keyframes).
        max_allowed = expected_duration + min(1.0, expected_duration * 0.01)
        min_allowed = max(1.0, expected_duration * 0.92)
        if out_duration > max_allowed:
            return (
                False,
                f"duration too long ({out_duration:.1f}s vs {expected_duration:.1f}s)",
                out_duration,
            )
        if out_duration < min_allowed:
            return (
                False,
                f"duration too short ({out_duration:.1f}s vs {expected_duration:.1f}s)",
                out_duration,
            )
        return True, "", out_duration
    if size >= min_bytes:
        return True, "", expected_duration
    return False, "cannot read output duration", None


def _finalize_output(temp_output: Path, output_path: Path) -> None:
    if output_path.is_file():
        output_path.unlink()
    shutil.move(str(temp_output), str(output_path))


def ffmpeg_trim(
    input_path: Path,
    start: float,
    end: float,
    output_path: Path,
    *,
    ffmpeg_exe: Path,
    log: Optional[ProgressCallback] = None,
) -> float:
    """Trim ``[start, end)`` in seconds. Returns output duration in seconds."""
    if end <= start:
        raise ValueError("end must be after start")

    duration = end - start
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_name(f"{output_path.stem}.part.mp4")
    if temp_output.is_file():
        temp_output.unlink()
    if output_path.is_file():
        output_path.unlink()

    video_encode = [
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "main",
        "-level",
        "4.0",
    ]
    tail = [
        "-movflags",
        "+faststart",
        "-avoid_negative_ts",
        "make_zero",
        "-reset_timestamps",
        "1",
        str(temp_output),
    ]

    # Do not use ``-map 0:v:0?`` — older ffmpeg builds reject it and produce no file.
    # ``-ss`` before ``-i`` lands on an earlier keyframe → extra seconds at the beginning.
    # Prefer trim/atrim filters for frame-accurate in/out after a coarse input seek.
    preroll_cap = 12.0
    input_seek = max(0.0, start - min(preroll_cap, start))
    trim_start = start - input_seek
    video_trim = (
        f"trim=start={trim_start:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS"
    )
    audio_trim = (
        f"atrim=start={trim_start:.3f}:duration={duration:.3f},asetpts=PTS-STARTPTS"
    )
    filter_seek = [
        "-ss",
        f"{input_seek:.3f}",
        "-i",
        str(input_path),
        "-vf",
        video_trim,
        "-af",
        audio_trim,
    ]
    accurate_seek = [
        "-i",
        str(input_path),
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
    ]
    aac_audio = [
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
    ]

    attempts: list[tuple[str, list[str], list[str]]] = [
        ("trim filter+aac", filter_seek, [*video_encode, *aac_audio]),
        ("accurate+aac", accurate_seek, [*video_encode, *aac_audio]),
        ("accurate+audio copy", accurate_seek, [*video_encode, "-c:a", "copy"]),
    ]

    last_error = "ffmpeg failed"
    for label, seek_args, encode_args in attempts:
        if temp_output.is_file():
            temp_output.unlink()
        cmd = [
            str(ffmpeg_exe),
            "-y",
            "-nostdin",
            "-hide_banner",
            *seek_args,
            *encode_args,
            *tail,
        ]
        if log:
            log(f"ffmpeg ({label})…")
        result = _run_ffmpeg(cmd)
        if result.returncode != 0 or not temp_output.is_file():
            last_error = f"{label}: {_ffmpeg_error(result)}"
            if log:
                log(last_error)
            continue

        ok, reason, out_duration = _output_looks_usable(
            temp_output, duration, ffmpeg_exe
        )
        if not ok:
            last_error = f"{label}: {reason}"
            if log:
                log(last_error)
            continue

        _finalize_output(temp_output, output_path)
        return out_duration if out_duration is not None else duration

    if temp_output.is_file():
        temp_output.unlink()
    raise RuntimeError(last_error)


def _remove_dir_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


class ClipRunner:
    def __init__(
        self,
        *,
        log: ProgressCallback,
        status: StatusCallback,
        cancel_check: Callable[[], bool],
    ) -> None:
        self._log = log
        self._status = status
        self._cancel_check = cancel_check

    def _report_overall(
        self,
        completed_units: float,
        total_units: int,
        detail: str,
    ) -> None:
        if total_units <= 0:
            self._status(0.0, detail)
            return
        pct = min(99.0, completed_units / total_units * 100.0)
        self._status(pct, f"总进度 {pct:.0f}% · {detail}")

    def _download_progress_hook(
        self,
        *,
        units_at_job_start: float,
        total_units: int,
        job_index: int,
        job_count: int,
        label: str,
    ) -> Callable[[dict], None]:
        last_logged_pct = [-1]

        def hook(data: dict) -> None:
            if self._cancel_check():
                raise DownloadCancelled()
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                downloaded = data.get("downloaded_bytes") or 0
                dl_ratio = (downloaded / total) if total else 0.0
                completed = units_at_job_start + dl_ratio
                speed = data.get("_speed_str") or ""
                eta = data.get("_eta_str") or ""
                short = label if len(label) <= 28 else label[:25] + "…"
                self._report_overall(
                    completed,
                    total_units,
                    f"[{job_index}/{job_count}] 下载 {short}",
                )
                pct_int = int(dl_ratio * 100)
                if pct_int >= last_logged_pct[0] + 2 or pct_int == 0:
                    last_logged_pct[0] = pct_int
                    total_mb = total / (1024 * 1024) if total else 0
                    got_mb = downloaded / (1024 * 1024) if downloaded else 0
                    self._log(
                        f"[download] {pct_int:3d}% of {total_mb:.2f}MiB "
                        f"at {speed} ETA {eta}"
                        if total
                        else f"[download] {got_mb:.2f}MiB at {speed}"
                    )
            elif status == "finished":
                filename = data.get("filename") or label
                self._log(f"下载完成: {filename}")

        return hook

    def _execute_segment_clips(
        self,
        *,
        source: Path,
        title: str,
        segments: list[ClipSegment],
        clips_dir: Path,
        source_duration: Optional[float],
        ffmpeg_exe: Path,
        total_segments: int,
        completed_units: float,
        total_units: int,
        done_segments: int,
    ) -> tuple[int, int, float, int]:
        """Trim all segments for one source file. Returns succeeded, failed, units, done count."""
        succeeded = 0
        failed = 0
        for seg_index, segment in enumerate(segments, start=1):
            if self._cancel_check():
                raise DownloadCancelled()

            start_label = format_timecode(segment.start).replace(":", "-")
            end_label = format_timecode(segment.end).replace(":", "-")
            folder_name = f"{title}_part{seg_index}_{start_label}_to_{end_label}"
            segment_dir = clips_dir / folder_name
            output_path = segment_dir / f"{folder_name}.mp4"

            clip_start = segment.start
            clip_end = segment.end
            if source_duration is not None:
                if clip_start >= source_duration:
                    failed += 1
                    done_segments += 1
                    completed_units += 1.0
                    self._log(
                        f"剪辑失败: 起点 {format_timecode(clip_start)} 超出视频时长 "
                        f"{format_timecode(source_duration)}"
                    )
                    self._report_overall(
                        completed_units,
                        total_units,
                        f"已完成 {done_segments}/{total_segments} 段",
                    )
                    continue
                if clip_end > source_duration:
                    self._log(
                        f"提示: 终点超出视频时长，已截断为 "
                        f"{format_timecode(source_duration)}"
                    )
                    clip_end = source_duration

            clip_duration = clip_end - clip_start
            seg_no = done_segments + 1
            self._report_overall(
                completed_units,
                total_units,
                f"剪辑 {seg_no}/{total_segments} "
                f"{format_timecode(clip_start)}–{format_timecode(clip_end)}",
            )
            self._log(
                f"剪辑 [{seg_no}/{total_segments}]: "
                f"{format_timecode(clip_start)} → {format_timecode(clip_end)} "
                f"（时长 {format_timecode(clip_duration)} / {clip_duration:.0f} 秒）"
            )

            try:
                out_seconds = ffmpeg_trim(
                    source,
                    clip_start,
                    clip_end,
                    output_path,
                    ffmpeg_exe=ffmpeg_exe,
                    log=self._log,
                )
                succeeded += 1
                self._log(f"已保存: {output_path}（约 {out_seconds:.1f} 秒）")
            except Exception as exc:
                failed += 1
                _remove_dir_if_empty(segment_dir)
                self._log(f"剪辑失败: {exc}")
            finally:
                done_segments += 1
                completed_units += 1.0
                self._report_overall(
                    completed_units,
                    total_units,
                    f"已完成 {done_segments}/{total_segments} 段",
                )
        return succeeded, failed, completed_units, done_segments

    def run_local(
        self,
        jobs: Iterable[ClipLocalJob],
        output_dir: Path,
    ) -> tuple[int, int]:
        """Clip local video files. Return ``(successful_clips, failed_clips)``."""
        env = inspect_environment(output_dir)
        if not env.ffmpeg_available:
            raise RuntimeError("ffmpeg is required for clipping")

        ffmpeg_exe = find_ffmpeg_executable()
        if ffmpeg_exe is None:
            raise RuntimeError("ffmpeg not found")
        ffprobe_exe = find_ffprobe_executable()

        job_list = list(jobs)
        if not job_list:
            raise ValueError("no clip jobs")

        clips_dir = output_dir
        clips_dir.mkdir(parents=True, exist_ok=True)

        total_segments = sum(len(job.segments) for job in job_list)
        job_count = len(job_list)
        total_units = max(1, total_segments)
        completed_units = 0.0
        done_segments = 0
        failed = 0
        succeeded = 0

        self._report_overall(0.0, total_units, "准备中…")

        for job_index, job in enumerate(job_list, start=1):
            if self._cancel_check():
                raise DownloadCancelled()

            source = job.source_path.resolve()
            if not source.is_file():
                self._log(f"[{job_index}/{job_count}] 文件不存在: {source}")
                failed += len(job.segments)
                done_segments += len(job.segments)
                completed_units += len(job.segments)
                continue
            if not is_local_video_file(source):
                self._log(
                    f"[{job_index}/{job_count}] 不支持的格式: {source.name}"
                )
                failed += len(job.segments)
                done_segments += len(job.segments)
                completed_units += len(job.segments)
                continue

            title = _sanitize_filename(source.stem)
            self._log(f"[{job_index}/{job_count}] 本地视频: {source.name}")
            source_duration = probe_source_duration(
                source,
                ffmpeg_exe=ffmpeg_exe,
                ffprobe_exe=ffprobe_exe,
            )
            if source_duration is not None:
                self._log(
                    f"源视频时长: {format_timecode(source_duration)} "
                    f"（{source_duration:.0f} 秒）"
                )

            seg_ok, seg_fail, completed_units, done_segments = (
                self._execute_segment_clips(
                    source=source,
                    title=title,
                    segments=job.segments,
                    clips_dir=clips_dir,
                    source_duration=source_duration,
                    ffmpeg_exe=ffmpeg_exe,
                    total_segments=total_segments,
                    completed_units=completed_units,
                    total_units=total_units,
                    done_segments=done_segments,
                )
            )
            succeeded += seg_ok
            failed += seg_fail

        summary = f"剪辑完成: 成功 {succeeded}，失败 {failed}"
        self._status(100.0, summary)
        self._log(summary)
        self._log(f"输出目录: {clips_dir}")
        return succeeded, failed

    def run(
        self,
        jobs: Iterable[ClipUrlJob],
        output_dir: Path,
        format_key: str,
        *,
        cache_dir: Path,
        cookie_file: Optional[Path] = None,
    ) -> tuple[int, int]:
        """Return ``(successful_clips, failed_clips)``."""
        ensure_ytdlp_on_path()
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError("yt-dlp is not installed") from exc

        env = inspect_environment(output_dir)
        if not env.yt_dlp_ready:
            raise RuntimeError("yt-dlp is not ready")
        if not env.ffmpeg_available:
            raise RuntimeError("ffmpeg is required for clipping")

        ffmpeg_exe = find_ffmpeg_executable()
        if ffmpeg_exe is None:
            raise RuntimeError("ffmpeg not found")
        ffprobe_exe = find_ffprobe_executable()

        job_list = list(jobs)
        if not job_list:
            raise ValueError("no clip jobs")

        clips_dir = output_dir
        temp_dir = cache_dir.resolve()
        clips_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        self._log(f"下载缓存目录: {temp_dir}")

        total_segments = sum(len(job.segments) for job in job_list)
        job_count = len(job_list)
        # One unit per URL download step + one unit per clip segment = full pipeline.
        total_units = max(1, total_segments + job_count)
        completed_units = 0.0
        done_segments = 0
        failed = 0
        succeeded = 0

        self._report_overall(0.0, total_units, "准备中…")

        for job_index, job in enumerate(job_list, start=1):
            if self._cancel_check():
                raise DownloadCancelled()

            url_kind = classify_url(job.url)
            if url_kind != "video":
                self._log(f"[{job_index}] 跳过（仅支持单个视频）: {job.url}")
                failed += len(job.segments)
                done_segments += len(job.segments)
                completed_units += len(job.segments) + 1.0
                continue

            units_at_job_start = completed_units
            self._log(f"[{job_index}/{job_count}] 下载: {job.url}")
            source_duration: Optional[float] = None
            opts = build_ydl_options(
                temp_dir,
                format_key,
                cookie_file=cookie_file,
                url_kind="video",
            )
            opts["outtmpl"] = str(temp_dir / "%(id)s.%(ext)s")
            opts["noplaylist"] = True
            opts["logger"] = _YtdlpLogger(self._log)

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(job.url, download=False)
                    if info is None:
                        raise RuntimeError("cannot read video info")
                    video_id = str(info.get("id") or "video")
                    title = _sanitize_filename(str(info.get("title") or video_id))
                    self._log(f"标题: {info.get('title') or title}")
                    source = _find_downloaded_file(temp_dir, video_id)
                    if source is None:
                        hook = self._download_progress_hook(
                            units_at_job_start=units_at_job_start,
                            total_units=total_units,
                            job_index=job_index,
                            job_count=job_count,
                            label=title,
                        )
                        ydl.params["progress_hooks"] = [hook]
                        self._report_overall(
                            units_at_job_start,
                            total_units,
                            f"[{job_index}/{job_count}] 下载 {title[:28]}",
                        )
                        with _hide_console_subprocess():
                            ydl.download([job.url])
                        source = _resolve_downloaded_file(
                            temp_dir, video_id, info, ydl
                        )
                        completed_units = units_at_job_start + 1.0
                        self._report_overall(
                            completed_units,
                            total_units,
                            f"[{job_index}/{job_count}] 下载完成",
                        )
                    else:
                        self._log(f"使用缓存视频: {source.name}")
                        completed_units = units_at_job_start + 1.0
                        self._report_overall(
                            completed_units,
                            total_units,
                            f"[{job_index}/{job_count}] 使用缓存",
                        )
                    if source is None:
                        _log_cache_dir_listing(temp_dir, video_id, self._log)
                        raise RuntimeError("download finished but file not found")
                    source_duration = probe_source_duration(
                        source,
                        ffmpeg_exe=ffmpeg_exe,
                        ffprobe_exe=ffprobe_exe,
                    )
                    if source_duration is not None:
                        self._log(
                            f"源视频时长: {format_timecode(source_duration)} "
                            f"（{source_duration:.0f} 秒）"
                        )
            except DownloadCancelled:
                raise
            except Exception as exc:
                self._log(f"下载失败: {job.url}\n  原因: {exc}")
                failed += len(job.segments)
                done_segments += len(job.segments)
                completed_units = units_at_job_start + 1.0 + len(job.segments)
                continue

            seg_ok, seg_fail, completed_units, done_segments = (
                self._execute_segment_clips(
                    source=source,
                    title=title,
                    segments=job.segments,
                    clips_dir=clips_dir,
                    source_duration=source_duration,
                    ffmpeg_exe=ffmpeg_exe,
                    total_segments=total_segments,
                    completed_units=completed_units,
                    total_units=total_units,
                    done_segments=done_segments,
                )
            )
            succeeded += seg_ok
            failed += seg_fail

        summary = f"剪辑完成: 成功 {succeeded}，失败 {failed}"
        self._status(100.0, summary)
        self._log(summary)
        self._log(f"输出目录: {clips_dir}")
        return succeeded, failed

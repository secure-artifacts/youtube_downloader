from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from dedupe import (
    build_output_template,
    classify_url,
    collect_existing_video_ids,
    make_skip_filter,
    record_downloaded_id,
    url_kind_label,
)
from paths_config import (
    APP_DIR,
    InstallPaths,
    ensure_ytdlp_on_path,
    find_ffmpeg_exe,
    find_ffprobe_exe,
    find_node_exe,
    ytdlp_is_ready,
)

TOOLS_DIR = APP_DIR / "tools"

ProgressCallback = Callable[[str], None]
StatusCallback = Callable[[float, str], None]


@dataclass
class EnvironmentInfo:
    yt_dlp_version: str
    yt_dlp_ready: bool
    ffmpeg_available: bool
    ffmpeg_path: Optional[str]
    ffmpeg_source: str
    js_runtimes: list[str]
    js_runtime_ready: bool
    js_runtime_source: str
    download_dir: Path
    missing: list[str]
    optional: list[str]


URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:"
    r"youtube\.com/(?:watch\?[^\s]+|playlist\?[^\s]+|@[^\s]+|channel/[^\s]+|c/[^\s]+|user/[^\s]+|shorts/[^\s]+)"
    r"|youtu\.be/[^\s]+"
    r")",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    found = URL_PATTERN.findall(text.strip())
    if found:
        return list(dict.fromkeys(found))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return list(dict.fromkeys(lines))


def default_download_dir() -> Path:
    home = Path.home()
    for candidate in (
        home / "Downloads" / "YouTube",
        home / "Downloads",
        Path(__file__).resolve().parent / "downloads",
    ):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path(__file__).resolve().parent / "downloads"


def find_node_executable(paths: Optional[InstallPaths] = None) -> Optional[Path]:
    return find_node_exe(paths)


def build_js_runtimes_dict(paths: Optional[InstallPaths] = None) -> dict[str, dict]:
    """Build yt-dlp js_runtimes option: {runtime: {config}}."""
    runtimes: dict[str, dict] = {}
    node = find_node_executable(paths)
    if node:
        runtimes["node"] = {"path": str(node)}
    for name in ("deno", "bun"):
        exe = shutil.which(name)
        if exe:
            runtimes[name] = {"path": exe}
    return runtimes


def detect_js_runtimes(paths: Optional[InstallPaths] = None) -> list[str]:
    labels: list[str] = []
    for name, config in build_js_runtimes_dict(paths).items():
        path = config.get("path")
        labels.append(f"{name}:{path}" if path else name)
    return labels


def detect_js_runtime_source(paths: Optional[InstallPaths] = None) -> tuple[bool, str]:
    from i18n import t

    install_paths = paths or InstallPaths.from_config()
    custom = install_paths.nodejs_dir / "node.exe"
    if custom.is_file():
        return True, t("env.node_custom", path=custom)
    system = find_node_exe(install_paths)
    if system and system != custom:
        return True, t("env.node_system", path=system)
    for name in ("deno", "bun"):
        path = shutil.which(name)
        if path:
            return True, t("env.runtime_path", name=name, path=path)
    return False, t("env.not_installed")


def find_ffmpeg_executable(paths: Optional[InstallPaths] = None) -> Optional[Path]:
    return find_ffmpeg_exe(paths)


def find_ffprobe_executable(paths: Optional[InstallPaths] = None) -> Optional[Path]:
    return find_ffprobe_exe(paths)


def get_ffmpeg_location() -> Optional[str]:
    exe = find_ffmpeg_executable()
    if not exe:
        return None
    return str(exe.parent)


def detect_ffmpeg() -> bool:
    return find_ffmpeg_executable() is not None


def detect_ffmpeg_source(paths: Optional[InstallPaths] = None) -> tuple[bool, Optional[str], str]:
    from i18n import t

    install_paths = paths or InstallPaths.from_config()
    custom = install_paths.ffmpeg_dir / "ffmpeg.exe"
    if custom.is_file():
        return True, str(custom), str(install_paths.ffmpeg_dir)
    system = shutil.which("ffmpeg")
    if system:
        return True, system, t("env.system_path")
    return False, None, t("env.not_installed")


def detect_yt_dlp_version(paths: Optional[InstallPaths] = None) -> tuple[str, bool]:
    ensure_ytdlp_on_path(paths)
    try:
        import yt_dlp

        return yt_dlp.version.__version__, True
    except Exception:
        pass
    install_paths = paths or InstallPaths.from_config()
    exe = install_paths.ytdlp_dir / "yt-dlp.exe"
    if exe.is_file():
        try:
            result = subprocess.run(
                [str(exe), "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            return (result.stdout or result.stderr or "未知").strip(), False
        except Exception:
            pass
    cli = shutil.which("yt-dlp")
    if cli:
        try:
            result = subprocess.run(
                [cli, "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            return (result.stdout or result.stderr or "未知").strip(), False
        except Exception:
            pass
    from i18n import t

    return t("env.not_installed"), False


def inspect_environment(
    download_dir: Optional[Path] = None,
    paths: Optional[InstallPaths] = None,
) -> EnvironmentInfo:
    install_paths = paths or InstallPaths.from_config()
    target = download_dir or default_download_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Keep user-selected path even if the folder does not exist yet.
        pass

    ensure_ytdlp_on_path(install_paths)
    yt_ready = ytdlp_is_ready(install_paths)
    if yt_ready:
        yt_version, _ = detect_yt_dlp_version(install_paths)
    else:
        from i18n import t

        yt_version = t("env.not_installed")

    ffmpeg_exe = find_ffmpeg_executable(install_paths)
    ffmpeg_ok = ffmpeg_exe is not None
    _, ffmpeg_path, ffmpeg_source = detect_ffmpeg_source(install_paths)
    if ffmpeg_ok and not ffmpeg_path:
        ffmpeg_path = str(ffmpeg_exe)

    js_ready, js_source = detect_js_runtime_source(install_paths)
    if not js_ready and find_node_executable(install_paths):
        from i18n import t

        js_ready = True
        node = find_node_executable(install_paths)
        js_source = t("env.node_system", path=node) if node else t("env.installed")

    missing: list[str] = []
    if not yt_ready:
        missing.append("yt-dlp")
    if not ffmpeg_ok:
        missing.append("ffmpeg")
    optional: list[str] = []
    if not js_ready:
        optional.append("Node.js")

    return EnvironmentInfo(
        yt_dlp_version=yt_version,
        yt_dlp_ready=yt_ready,
        ffmpeg_available=ffmpeg_ok,
        ffmpeg_path=ffmpeg_path,
        ffmpeg_source=ffmpeg_source,
        js_runtimes=detect_js_runtimes(install_paths),
        js_runtime_ready=js_ready,
        js_runtime_source=js_source,
        download_dir=target,
        missing=missing,
        optional=optional,
    )


def format_label(format_key: str) -> str:
    from i18n import format_label_for_key

    return format_label_for_key(format_key)


def build_format_selector(format_key: str) -> str:
    if format_key == "best":
        return "bestvideo+bestaudio/best"
    if format_key == "2160":
        return "bestvideo[height<=2160]+bestaudio/best[height<=2160]"
    if format_key == "1440":
        return "bestvideo[height<=1440]+bestaudio/best[height<=1440]"
    if format_key == "1080":
        return "bestvideo[height<=1080]+bestaudio/best[height<=1080]"
    if format_key == "720":
        return "bestvideo[height<=720]+bestaudio/best[height<=720]"
    if format_key == "480":
        return "bestvideo[height<=480]+bestaudio/best[height<=480]"
    if format_key == "360":
        return "bestvideo[height<=360]+bestaudio/best[height<=360]"
    if format_key == "240":
        return "bestvideo[height<=240]+bestaudio/best[height<=240]"
    if format_key == "audio":
        return "bestaudio/best"
    return "bestvideo+bestaudio/best"


def build_ydl_options(
    output_dir: Path,
    format_key: str,
    *,
    subtitles: bool = False,
    cookie_file: Optional[Path] = None,
    url_kind: str = "video",
    existing_ids: Optional[set[str]] = None,
    skip_log: Optional[ProgressCallback] = None,
    skipped_counter: Optional[list[int]] = None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    opts: dict = {
        "outtmpl": build_output_template(output_dir, url_kind),
        "merge_output_format": "mp4",
        "noplaylist": url_kind == "video",
        "ignoreerrors": url_kind in ("playlist", "channel"),
        "retries": 3,
        "fragment_retries": 3,
        "continuedl": True,
        "windowsfilenames": True,
        "format": build_format_selector(format_key),
        "restrictfilenames": False,
        "nocheckcertificate": False,
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "default",
                    "tv",
                    "web_safari",
                ],
            }
        },
    }

    if existing_ids is not None and skip_log is not None and skipped_counter is not None:
        opts["match_filter"] = make_skip_filter(
            existing_ids,
            skip_log,
            skipped_counter,
            directory=output_dir,
            url_kind=url_kind,
        )

    js_runtimes = build_js_runtimes_dict()
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes

    ffmpeg_location = get_ffmpeg_location()
    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    if format_key == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    if subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = ["zh-Hans", "zh-Hant", "zh", "en", "ru", "uk"]
        opts["subtitlesformat"] = "srt/best"

    if cookie_file and cookie_file.is_file():
        opts["cookiefile"] = str(cookie_file)

    return opts


class DownloadCancelled(Exception):
    pass


def explain_download_error(message: str) -> Optional[str]:
    """Map a yt-dlp error string to actionable Chinese guidance, if recognized."""
    low = message.lower()
    if "sign in to confirm" in low or "not a bot" in low or "confirm you" in low:
        return (
            "YouTube 要求验证身份（防机器人）。请在浏览器登录后导出 cookies.txt，"
            "用「Cookies 文件」右侧的「选择文件…」选中它再下载。"
        )
    if "private video" in low or "this video is private" in low:
        return (
            "这是私享视频，必须用「有权限观看该视频的账号」导出的 cookies.txt 才能下载。"
        )
    if "members-only" in low or "members only" in low or "join this channel" in low:
        return (
            "这是会员专属视频。请用已加入该频道会员的账号导出 cookies.txt 后再下载。"
        )
    if "login required" in low or "requires login" in low or "account" in low and "cookies" in low:
        return (
            "该视频需要登录。请导出浏览器 cookies.txt，用「选择文件…」选中后再下载。"
        )
    if "age" in low and ("restrict" in low or "confirm" in low):
        return (
            "这是年龄限制视频。请用已登录且已确认年龄的账号导出 cookies.txt 后再下载。"
        )
    if "video unavailable" in low or "not available" in low:
        return (
            "视频不可用：可能是地区限制、已被删除/设为私有，或需要登录。"
            "若你在浏览器能正常播放，请导出该账号的 cookies.txt 后再试。"
        )
    if "unable to extract" in low or "unable to download webpage" in low or "http error 403" in low:
        return (
            "解析失败，通常是 yt-dlp 版本过旧。请点「一键安装依赖」更新 yt-dlp 后重试；"
            "若仍失败，请导出 cookies.txt 再试。"
        )
    return None


class Downloader:
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
        self._current_title = ""
        self._record_dir: Optional[Path] = None

    def _hook(self, data: dict) -> None:
        if self._cancel_check():
            raise DownloadCancelled()

        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = (downloaded / total * 100) if total else 0.0
            speed = data.get("_speed_str") or ""
            eta = data.get("_eta_str") or ""
            prefix = self._current_title or "下载中"
            self._status(percent, f"{prefix}  {percent:.1f}%  {speed}  ETA {eta}")
        elif status == "finished":
            filename = data.get("filename") or ""
            self._log(f"已完成: {filename}")
            if self._record_dir is not None:
                info = data.get("info_dict") or {}
                record_downloaded_id(self._record_dir, info.get("id"))

    def download_urls(
        self,
        urls: Iterable[str],
        output_dir: Path,
        format_key: str,
        *,
        subtitles: bool = False,
        cookie_file: Optional[Path] = None,
    ) -> tuple[int, int, int]:
        """Return ``(successful_urls, failed_urls, skipped_videos)``."""
        ensure_ytdlp_on_path()
        try:
            import yt_dlp
        except ImportError as exc:
            raise RuntimeError(
                "未找到 yt-dlp。请选择安装目录后点击「↓ yt-dlp」安装。"
            ) from exc

        url_list = list(urls)
        if not url_list:
            raise ValueError("请先输入至少一个 YouTube 链接。")

        self._record_dir = output_dir

        env = inspect_environment(output_dir)
        if format_key != "audio" and not env.ffmpeg_available:
            self._log("提示: 未检测到 ffmpeg。可点击「一键安装依赖」自动下载。")

        if not env.yt_dlp_ready:
            raise RuntimeError(
                "未安装 yt-dlp。请点击「一键安装依赖」，或运行 setup.bat。"
            )

        if not env.js_runtimes:
            self._log(
                "提示: 未检测到 Node.js。可点击「安装 Node.js」自动从 nodejs.org 下载。"
            )

        success = 0
        failed = 0
        total_skipped = 0

        for index, url in enumerate(url_list, start=1):
            if self._cancel_check():
                raise DownloadCancelled()

            url_kind = classify_url(url)
            existing_ids = collect_existing_video_ids(output_dir, url_kind=url_kind)
            skipped_counter = [0]

            self._log(f"[{index}/{len(url_list)}] 开始 ({url_kind_label(url_kind)}): {url}")
            if existing_ids:
                if url_kind == "channel":
                    playlist_dirs = [
                        p.name for p in output_dir.iterdir() if p.is_dir()
                    ]
                    if playlist_dirs:
                        preview = "、".join(playlist_dirs[:4])
                        if len(playlist_dirs) > 4:
                            preview += "…"
                        self._log(
                            f"去重：已扫描保存目录及 {len(playlist_dirs)} 个播放列表子文件夹"
                            f"（{preview}），共 {len(existing_ids)} 个已下载视频"
                        )
                    else:
                        self._log(
                            f"去重：已扫描保存目录，共 {len(existing_ids)} 个已下载视频"
                        )
                else:
                    self._log(
                        f"去重：当前文件夹已有 {len(existing_ids)} 个视频，将跳过重复项"
                    )
            else:
                self._log("去重：未发现已下载视频，将全部下载")

            opts = build_ydl_options(
                output_dir,
                format_key,
                subtitles=subtitles,
                cookie_file=cookie_file,
                url_kind=url_kind,
                existing_ids=existing_ids,
                skip_log=self._log,
                skipped_counter=skipped_counter,
            )
            opts["progress_hooks"] = [self._hook]
            opts["logger"] = _YtdlpLogger(self._log)

            self._current_title = f"[{index}/{len(url_list)}]"
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info is None:
                        raise RuntimeError("无法读取视频信息")

                    info_type = info.get("_type")
                    if info_type in ("playlist", "multi_video"):
                        title = info.get("title") or "播放列表"
                        entries = info.get("entries") or []
                        count = len(entries) if entries else info.get("playlist_count") or "?"
                        self._log(f"播放列表: {title}（约 {count} 项）")
                        if url_kind == "channel":
                            self._log("频道模式：按播放列表分子文件夹保存")
                    elif info_type == "channel":
                        title = info.get("title") or "频道"
                        self._log(f"频道: {title}")
                        self._log("频道模式：按播放列表分子文件夹保存")
                    else:
                        title = info.get("title") or url
                        self._current_title = title
                        self._log(f"标题: {title}")

                    ydl.download([url])

                skipped = skipped_counter[0]
                total_skipped += skipped
                if skipped:
                    self._log(f"本链接跳过 {skipped} 个已存在视频")
                success += 1
                self._log(f"完成: {url}")
            except DownloadCancelled:
                raise
            except Exception as exc:
                failed += 1
                self._log(f"失败: {url}\n  原因: {exc}")
                hint = explain_download_error(str(exc))
                if hint:
                    self._log(f"  解决建议: {hint}")

        summary = "全部完成"
        if failed:
            summary = f"完成，失败 {failed} 个链接"
        if total_skipped:
            summary += f"，跳过 {total_skipped} 个重复视频"
        self._status(100.0, summary)
        return success, failed, total_skipped


class _YtdlpLogger:
    def __init__(self, log: ProgressCallback) -> None:
        self._log = log

    def _emit(self, message: str, *, prefix: str = "") -> None:
        text = message.rstrip("\r")
        if not text:
            return
        for line in text.splitlines():
            line = line.strip("\r")
            if not line:
                continue
            self._log(f"{prefix}{line}" if prefix else line)

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            return
        self._emit(message)

    def info(self, message: str) -> None:
        self._emit(message)

    def warning(self, message: str) -> None:
        self._emit(message, prefix="警告: ")

    def error(self, message: str) -> None:
        self._emit(message, prefix="错误: ")

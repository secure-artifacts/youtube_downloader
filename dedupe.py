"""Detect already-downloaded YouTube videos and skip duplicates."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

VIDEO_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
CHANNEL_URL = re.compile(
    r"youtube\.com/(?:@[\w.-]+(?:/[\w.-]+)?|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)",
    re.IGNORECASE,
)

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mp3",
    ".m4a",
    ".opus",
    ".wav",
    ".flv",
    ".avi",
    ".mov",
}


def classify_url(url: str) -> str:
    """Return ``video``, ``playlist``, or ``channel``."""
    lower = url.strip().lower()
    if CHANNEL_URL.search(lower):
        return "channel"
    if "list=" in lower:
        return "playlist"
    return "video"


TRAILING_ID = re.compile(r"\s*\[[A-Za-z0-9_-]{11}\]$")


def video_id_from_filename(name: str) -> Optional[str]:
    matches = VIDEO_ID_IN_NAME.findall(name)
    if not matches:
        return None
    return matches[-1]


def _sanitize_title(title: str) -> str:
    """Approximate yt-dlp's filename sanitization for collision checks."""
    try:
        from yt_dlp.utils import sanitize_filename

        return sanitize_filename(title, restricted=False)
    except Exception:
        return re.sub(r'[\\/:*?"<>|]+', "_", title).strip()


def _existing_title_stems(directory: Path, url_kind: str) -> set[str]:
    """Collect title stems (without trailing ``[id]``) of files already on disk."""
    stems: set[str] = set()
    if not directory.is_dir():
        return stems
    folders = [directory]
    if url_kind == "channel":
        folders += [p for p in directory.iterdir() if p.is_dir()]
    for folder in folders:
        try:
            items = list(folder.iterdir())
        except OSError:
            continue
        for item in items:
            if not item.is_file():
                continue
            if item.suffix.lower() not in MEDIA_EXTENSIONS and item.suffix:
                continue
            stems.add(TRAILING_ID.sub("", item.stem))
    return stems


def _scan_folder(folder: Path, ids: set[str]) -> None:
    try:
        items = list(folder.iterdir())
    except OSError:
        return
    for item in items:
        if not item.is_file():
            continue
        if item.suffix.lower() not in MEDIA_EXTENSIONS and item.suffix:
            continue
        vid = video_id_from_filename(item.name)
        if vid:
            ids.add(vid)


def collect_existing_video_ids(
    directory: Path,
    *,
    url_kind: str = "video",
) -> set[str]:
    """
    Collect YouTube video IDs from filenames like ``Title [dQw4w9WgXcQ].mp4``.

    - playlist / video: only files directly in ``directory``
    - channel: files inside each playlist subfolder, plus files in ``directory``
    """
    ids: set[str] = set()
    if not directory.is_dir():
        return ids

    _read_archive_ids(directory, ids)

    if url_kind in ("video", "playlist"):
        _scan_folder(directory, ids)
        return ids

    for item in directory.iterdir():
        if item.is_dir():
            _scan_folder(item, ids)
    _scan_folder(directory, ids)
    return ids


def build_output_template(output_dir: Path, url_kind: str) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    if url_kind == "channel":
        return str(
            output_dir / "%(playlist_title|Uploads)s" / "%(title)s%(id_tag|)s.%(ext)s"
        )
    return str(output_dir / "%(title)s%(id_tag|)s.%(ext)s")


def download_archive_path(directory: Path) -> Path:
    """Sidecar file that records downloaded video IDs for dedupe."""
    return directory / ".downloaded_ids.txt"


def _read_archive_ids(directory: Path, ids: set[str]) -> None:
    path = download_archive_path(directory)
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                token = line.strip().split()[-1] if line.strip() else ""
                if re.fullmatch(r"[A-Za-z0-9_-]{11}", token):
                    ids.add(token)
    except OSError:
        return


def record_downloaded_id(directory: Path, video_id: Optional[str]) -> None:
    """Append a video ID to the sidecar archive (skipping duplicates)."""
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return
    existing: set[str] = set()
    _read_archive_ids(directory, existing)
    if video_id in existing:
        return
    path = download_archive_path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"youtube {video_id}\n")
    except OSError:
        return


def make_skip_filter(
    existing_ids: set[str],
    log: Callable[[str], None],
    skipped_counter: list[int],
    *,
    directory: Optional[Path] = None,
    url_kind: str = "video",
) -> Callable:
    """Build a yt-dlp ``match_filter`` callback.

    Besides skipping already-downloaded videos, it sets ``info['id_tag']`` so the
    output template only appends ``[video_id]`` when a clean title would collide
    with an existing file (or another video in the same batch).
    """

    existing_stems = (
        _existing_title_stems(directory, url_kind) if directory is not None else set()
    )
    session_stems: set[str] = set()
    decided: dict[str, str] = {}

    def skip_filter(info: dict, *, incomplete: bool = False) -> Optional[str]:
        vid = info.get("id")
        if vid and vid in existing_ids:
            title = info.get("title") or vid
            playlist = info.get("playlist_title") or info.get("playlist") or ""
            if playlist:
                log(f"跳过（已存在）: {title} [{vid}]  ← 播放列表「{playlist}」")
            else:
                log(f"跳过（已存在）: {title} [{vid}]")
            skipped_counter[0] += 1
            return "already downloaded"

        if vid and vid in decided:
            info["id_tag"] = decided[vid]
            return None

        stem = _sanitize_title(info.get("title") or "")
        if stem and (stem in existing_stems or stem in session_stems) and vid:
            tag = f" [{vid}]"
        else:
            tag = ""
            if stem:
                session_stems.add(stem)
        info["id_tag"] = tag
        if vid:
            decided[vid] = tag
        return None

    return skip_filter


def url_kind_label(url_kind: str) -> str:
    return {"video": "单个视频", "playlist": "播放列表", "channel": "频道"}.get(
        url_kind, url_kind
    )

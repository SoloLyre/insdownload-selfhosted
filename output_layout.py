from __future__ import annotations

import re
import time
from pathlib import Path


MASTER_DIR_NAME = "platform_downloads"
PHOTO_DIR_NAME = "photo"
VIDEO_DIR_NAME = "video"
AUDIO_DIR_NAME = "audio"
OTHER_DIR_NAME = "other"
LEGACY_MEDIA_DIR_NAMES = ("图片", "视频", "音频", "其他")
PLATFORM_DIRS = {
    "ins": "ins",
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
}
DATE_SUFFIX_RE = re.compile(r"_\d{4}-\d{2}-\d{2}(?:_\d+)?$")


def workspace_root() -> Path:
    return Path(__file__).resolve().parent


def default_platform_root(platform: str) -> Path:
    try:
        platform_dir = PLATFORM_DIRS[platform]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {platform}") from exc
    return workspace_root() / MASTER_DIR_NAME / platform_dir


def sanitize_folder_name(name: str, default: str = "download") -> str:
    cleaned = "".join("_" if char in '<>:"/\\|?*\0' else char for char in (name or "")).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned or default


def looks_like_final_output_dir(path: Path) -> bool:
    if DATE_SUFFIX_RE.search(path.name):
        return True
    if (path / "download_manifest.json").exists():
        return True
    for folder_name in (
        PHOTO_DIR_NAME,
        VIDEO_DIR_NAME,
        AUDIO_DIR_NAME,
        OTHER_DIR_NAME,
        *LEGACY_MEDIA_DIR_NAMES,
    ):
        if (path / folder_name).exists():
            return True
    return False


def dated_folder_name(
    account_name: str,
    default_name: str = "download",
) -> str:
    safe_name = sanitize_folder_name(account_name, default_name)
    if DATE_SUFFIX_RE.search(safe_name):
        return safe_name
    return f"{safe_name}_{time.strftime('%Y-%m-%d')}"


def allocate_output_dir(
    platform: str,
    output_dir: str | None,
    account_name: str,
    *,
    default_name: str = "download",
) -> Path:
    if output_dir:
        requested = Path(output_dir).expanduser().resolve()
        if looks_like_final_output_dir(requested):
            return requested
        root = requested
    else:
        root = default_platform_root(platform)

    root.mkdir(parents=True, exist_ok=True)
    base_name = dated_folder_name(
        account_name,
        default_name=default_name,
    )
    candidate = root / base_name
    if not candidate.exists():
        return candidate

    counter = 2
    while True:
        numbered = root / f"{base_name}_{counter}"
        if not numbered.exists():
            return numbered
        counter += 1

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".heic"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@dataclass(slots=True)
class PlatformAdapter:
    platform_id: str
    label: str
    output_platform_key: str
    build_command: Callable[[dict, object, Path], list[str]]
    collect_result: Callable[[dict, Path, Path], dict]


def discover_output_dir(output_root: Path, before_snapshot: set[str]) -> Path | None:
    if not output_root.exists():
        return None
    candidates = []
    for child in output_root.iterdir():
        if child.is_dir() and str(child.resolve()) not in before_snapshot:
            candidates.append(child)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def read_manifest(manifest_path: Path) -> dict | None:
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def count_media_files(output_dir: Path) -> tuple[int, int]:
    image_count = 0
    video_count = 0
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            image_count += 1
        elif suffix in VIDEO_SUFFIXES:
            video_count += 1
    return image_count, video_count

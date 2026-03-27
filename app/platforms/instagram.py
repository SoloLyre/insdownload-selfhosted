from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from app.platforms.base import PlatformAdapter, count_media_files, read_manifest


def _extract_username(raw_url: str) -> str:
    if "instagram.com" not in raw_url:
        return raw_url.strip().lstrip("@").strip("/") or "instagram_user"
    parsed = urlparse(raw_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return "instagram_user"
    if segments[0].lower() in {"p", "reel", "reels", "tv"}:
        return (segments[1] if len(segments) > 1 else "instagram_post").lstrip("@")
    return segments[0].lstrip("@")


def build_instagram_command(task: dict, settings: object, output_root: Path) -> list[str]:
    command = [
        sys.executable,
        "download_instagram.py",
        task["input_url"],
        "--target-kind",
        task.get("target_kind") or "profile",
        "--output-dir",
        str(output_root),
    ]
    if task.get("browser"):
        command.extend(["--browser", task["browser"]])
        command.extend(
            [
                "--browser-profile",
                task.get("browser_profile") or getattr(settings.browser, "default_profile", "Default"),
            ]
        )
    return command


def collect_instagram_result(task: dict, output_root: Path, output_dir: Path) -> dict:
    manifest_path = output_dir / "download_manifest.json"
    manifest = read_manifest(manifest_path)
    if manifest is None:
        images, videos = count_media_files(output_dir)
        manifest = {
            "target_kind": task.get("target_kind") or "profile",
            "username": _extract_username(task["input_url"]),
            "input_url": task["input_url"],
            "images_downloaded": images,
            "videos_downloaded": videos,
            "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


INSTAGRAM_ADAPTER = PlatformAdapter(
    platform_id="instagram",
    label="Instagram",
    output_platform_key="ins",
    build_command=build_instagram_command,
    collect_result=collect_instagram_result,
)

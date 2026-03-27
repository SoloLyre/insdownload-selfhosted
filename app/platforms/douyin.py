from __future__ import annotations

import json
import sys
from pathlib import Path

from app.platforms.base import PlatformAdapter, count_media_files, read_manifest


def build_douyin_command(task: dict, settings: object, output_root: Path) -> list[str]:
    browser = task.get("browser") or getattr(settings.browser, "default_browser", "chrome")
    browser_profile = task.get("browser_profile") or getattr(settings.browser, "default_profile", "Default")
    return [
        sys.executable,
        "download_douyin_profile.py",
        task["input_url"],
        "--target-kind",
        task.get("target_kind") or "profile",
        "--browser",
        browser,
        "--browser-profile",
        browser_profile,
        "--output-dir",
        str(output_root),
    ]


def collect_douyin_result(task: dict, output_root: Path, output_dir: Path) -> dict:
    manifest_path = output_dir / "download_manifest.json"
    manifest = read_manifest(manifest_path)
    if manifest is None:
        images, videos = count_media_files(output_dir)
        manifest = {
            "target_kind": task.get("target_kind") or "profile",
            "input_url": task["input_url"],
            "images_downloaded": images,
            "videos_downloaded": videos,
            "failed_items": 0,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


DOUYIN_ADAPTER = PlatformAdapter(
    platform_id="douyin",
    label="Douyin",
    output_platform_key="douyin",
    build_command=build_douyin_command,
    collect_result=collect_douyin_result,
)

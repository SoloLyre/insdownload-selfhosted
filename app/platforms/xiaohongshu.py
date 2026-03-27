from __future__ import annotations

import json
import sys
from pathlib import Path

from app.platforms.base import PlatformAdapter, count_media_files, read_manifest


def build_xiaohongshu_command(task: dict, settings: object, output_root: Path) -> list[str]:
    browser_profile = task.get("browser_profile") or getattr(settings.browser, "default_profile", "Default")
    return [
        sys.executable,
        "download_xiaohongshu.py",
        task["input_url"],
        "--target-kind",
        task.get("target_kind") or "profile",
        "--chrome-profile",
        browser_profile,
        "--output-dir",
        str(output_root),
    ]


def collect_xiaohongshu_result(task: dict, output_root: Path, output_dir: Path) -> dict:
    manifest_path = output_dir / "download_manifest.json"
    manifest = read_manifest(manifest_path)
    if manifest is None:
        images, videos = count_media_files(output_dir)
        manifest = {
            "target_kind": task.get("target_kind") or "profile",
            "input_url": task["input_url"],
            "stats": {
                "images_downloaded": images,
                "videos_downloaded": videos,
                "notes_total": None,
                "notes_ok": None,
                "notes_failed": None,
                "skipped_existing": 0,
                "media_failed": 0,
            },
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "output_dir": str(output_dir),
        "manifest_path": str(manifest_path),
        "manifest": manifest,
    }


XIAOHONGSHU_ADAPTER = PlatformAdapter(
    platform_id="xiaohongshu",
    label="Xiaohongshu",
    output_platform_key="xiaohongshu",
    build_command=build_xiaohongshu_command,
    collect_result=collect_xiaohongshu_result,
)

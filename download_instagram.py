#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import instaloader

from download_instagram_gallery import build_command as build_gallery_profile_command
from input_parsing import extract_url_from_text
from output_layout import allocate_output_dir


SUPPORTED_BROWSERS = [
    "safari",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "brave",
    "opera",
    "opera_gx",
    "vivaldi",
]
POST_PATH_PREFIXES = {"p", "reel", "reels", "tv"}
TARGET_CHOICES = ("auto", "profile", "single")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".avif", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}


def normalize_instagram_target(target: str) -> str:
    raw = (target or "").strip()
    if not raw:
        raise ValueError("Instagram target URL or identifier cannot be empty.")
    lowered = raw.lower()
    if "instagram.com" in lowered or "instagr.am" in lowered:
        return extract_url_from_text(
            raw,
            ("instagram.com", "instagr.am"),
            field_name="Instagram target",
        )
    return raw


def find_instaloader_binary() -> str:
    candidates = [
        Path(sys.executable).parent / "instaloader",
        shutil.which("instaloader"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
    raise FileNotFoundError(
        "instaloader not found. Install it into the active environment or add it to PATH."
    )


def detect_target_kind(target: str) -> str:
    target = normalize_instagram_target(target)
    if "instagram.com" not in target:
        return "profile"

    parsed = urlparse(target)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and segments[0].lower() in POST_PATH_PREFIXES:
        return "single"
    return "profile"


def extract_username(profile: str) -> str:
    profile = normalize_instagram_target(profile)

    if "instagram.com" not in profile:
        return profile.lstrip("@").strip("/")

    parsed = urlparse(profile)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise ValueError("Could not find a username in the Instagram URL.")
    if segments[0].lower() in POST_PATH_PREFIXES:
        raise ValueError("This Instagram URL points to a single post, not a profile.")
    return segments[0].lstrip("@")


def extract_shortcode(target: str) -> str:
    target = normalize_instagram_target(target)

    if "instagram.com" not in target:
        shortcode = target.lstrip("-").strip("/")
        if shortcode:
            return shortcode
        raise ValueError("Could not find a shortcode in the Instagram target.")

    parsed = urlparse(target)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2 or segments[0].lower() not in POST_PATH_PREFIXES:
        raise ValueError("This Instagram URL does not look like a post or reel URL.")
    shortcode = segments[1].lstrip("-")
    if not shortcode:
        raise ValueError("Could not find a shortcode in the Instagram URL.")
    return shortcode


def build_common_command(
    output_dir: Path,
    browser: str | None,
    login_username: str | None,
    no_iphone: bool,
) -> list[str]:
    common = [
        find_instaloader_binary(),
        "--dirname-pattern",
        str(output_dir),
        "--sanitize-paths",
        "--max-connection-attempts",
        "5",
        "--request-timeout",
        "120",
        "--no-metadata-json",
        "--no-captions",
    ]

    if browser:
        common.extend(
            [
                "--load-cookies",
                browser,
            ]
        )
    elif login_username:
        common.extend(
            [
                "--login",
                login_username,
            ]
        )

    if no_iphone:
        common.append("--no-iphone")

    return common


def build_profile_command(
    username: str,
    output_dir: Path,
    browser: str | None,
    browser_profile: str | None,
    login_username: str | None,
    no_iphone: bool,
) -> list[str]:
    if browser:
        gallery_args = SimpleNamespace(
            browser=browser,
            browser_profile=browser_profile or "Default",
            domain="instagram.com",
            filename="{media_id!S}.{extension}",
            include="avatar,posts,reels,stories,highlights",
            simulate=False,
        )
        return build_gallery_profile_command(gallery_args, username, output_dir)

    command = build_common_command(output_dir, browser, login_username, no_iphone)
    # IGTV retrieval is unstable in current Instaloader releases and can fail
    # after media already downloaded. Keep reels enabled, skip IGTV for stability.
    command.extend(["--reels"])
    if browser or login_username:
        command.extend(["--stories", "--highlights"])
    command.append(username)
    return command


def build_single_command(
    shortcode: str,
    output_dir: Path,
    browser: str | None,
    login_username: str | None,
    no_iphone: bool,
) -> list[str]:
    command = build_common_command(output_dir, browser, login_username, no_iphone)
    command.extend(["--", f"-{shortcode}"])
    return command


def resolve_single_post_owner_username(shortcode: str) -> str | None:
    try:
        loader = instaloader.Instaloader(
            quiet=True,
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
        )
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        owner_username = (post.owner_username or "").strip()
        return owner_username or None
    except Exception:
        return None


def safe_media_destination(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        numbered = directory / f"{stem}_{counter}{suffix}"
        if not numbered.exists():
            return numbered
        counter += 1


def classify_instagram_media(
    source_dir: Path,
    final_dir: Path | None = None,
) -> tuple[int, int]:
    if final_dir is None:
        final_dir = source_dir

    image_dir = final_dir / "图片"
    video_dir = final_dir / "视频"
    skip_dirs = {image_dir.resolve(), video_dir.resolve()}

    moved_images = 0
    moved_videos = 0
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(parent.resolve() in skip_dirs for parent in path.parents):
            continue
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            image_dir.mkdir(parents=True, exist_ok=True)
            destination = safe_media_destination(image_dir, path.name)
            path.replace(destination)
            moved_images += 1
        elif suffix in VIDEO_SUFFIXES:
            video_dir.mkdir(parents=True, exist_ok=True)
            destination = safe_media_destination(video_dir, path.name)
            path.replace(destination)
            moved_videos += 1

    return moved_images, moved_videos


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Instagram media in the highest quality Instagram currently "
            "serves. Public posts, reels, IGTV, and profile picture work without "
            "login. Stories and highlights require a logged-in session."
        )
    )
    parser.add_argument(
        "target",
        help="Instagram profile URL/username or post/reel URL.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Root directory where a dated profile folder should be created. "
            "If you point this at an existing dated download folder, files are written there directly."
        ),
    )
    parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        help="Load Instagram cookies from a logged-in browser to include stories and highlights.",
    )
    parser.add_argument(
        "--browser-profile",
        default="Default",
        help="Browser profile name to use with --browser. Defaults to Default.",
    )
    parser.add_argument(
        "--login",
        dest="login_username",
        help="Instagram username to log in with. Instaloader will prompt for password/2FA if needed.",
    )
    parser.add_argument(
        "--no-iphone",
        action="store_true",
        help="Use web endpoints only. This can help when Instagram rate-limits the default iPhone endpoints.",
    )
    parser.add_argument(
        "--target-kind",
        choices=TARGET_CHOICES,
        default="auto",
        help="Choose whether the target is a profile or a single post. Defaults to auto.",
    )
    args = parser.parse_args()

    if args.browser and args.login_username:
        parser.error("--browser and --login cannot be used together.")

    return args


def main() -> int:
    args = parse_args()

    try:
        target_kind = args.target_kind
        if target_kind == "auto":
            target_kind = detect_target_kind(args.target)

        if target_kind == "single":
            shortcode = extract_shortcode(args.target)
            output_name = resolve_single_post_owner_username(shortcode) or f"instagram_post_{shortcode}"
            output_dir = allocate_output_dir(
                "ins",
                args.output_dir,
                output_name,
                default_name="instagram_post",
            )
        else:
            username = extract_username(args.target)
            output_dir = allocate_output_dir(
                "ins",
                args.output_dir,
                username,
                default_name="instagram_user",
            )
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir / ".staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    downloader_label = "gallery-dl" if target_kind == "profile" and args.browser else "instaloader"

    if target_kind == "single":
        command = build_single_command(
            shortcode=shortcode,
            output_dir=staging_dir,
            browser=args.browser,
            login_username=args.login_username,
            no_iphone=args.no_iphone,
        )
    else:
        command = build_profile_command(
            username=username,
            output_dir=staging_dir,
            browser=args.browser,
            browser_profile=args.browser_profile,
            login_username=args.login_username,
            no_iphone=args.no_iphone,
        )

    if args.browser:
        print(
            f"Using browser cookies from '{args.browser}'."
        )
    elif args.login_username:
        print("Using Instagram session login.")
    else:
        print("No login provided. Private targets or stories/highlights will not be available.")

    print(f"Target kind: {target_kind}")
    print(f"Output directory: {output_dir}")

    print(f"Running {downloader_label}...")

    env = os.environ.copy()
    env["PYTHONWARNINGS"] = "ignore"

    completed = subprocess.run(command, env=env)

    moved_images = 0
    moved_videos = 0
    if staging_dir.exists():
        moved_images, moved_videos = classify_instagram_media(staging_dir, output_dir)
        shutil.rmtree(staging_dir, ignore_errors=True)
        if moved_images or moved_videos:
            print(f"Images {moved_images}, videos {moved_videos}")

    if completed.returncode != 0:
        if moved_images == 0 and moved_videos == 0:
            print("No media downloaded.")
        return completed.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

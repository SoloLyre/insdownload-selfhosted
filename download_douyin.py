#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from output_layout import (
    AUDIO_DIR_NAME,
    OTHER_DIR_NAME,
    PHOTO_DIR_NAME,
    VIDEO_DIR_NAME,
    allocate_output_dir,
)


SUPPORTED_BROWSERS = [
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
    "whale",
]
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
DOUYIN_HOST_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)?(?:douyin|iesdouyin)\.com/[^\s]+", re.I)
VIDEO_ID_RE = re.compile(r"/video/(?P<id>\d+)")
NOTE_ID_RE = re.compile(r"/note/(?P<id>\d+)")
TRIM_CHARS = " \t\r\n\"'`<>[](){}。，、；：！？,.!?:;）】》"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Douyin media in the highest quality currently exposed by "
            "Douyin/yt-dlp. Fresh browser cookies are usually required."
        )
    )
    parser.add_argument(
        "share_text_or_url",
        help="Douyin share text, short link, or standard video URL.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Root directory where a dated download folder should be created. "
            "If you point this at an existing dated download folder, files are written there directly."
        ),
    )
    parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        help="Load fresh Douyin cookies from this browser. Auto-detected when omitted.",
    )
    parser.add_argument(
        "--browser-profile",
        help="Browser profile for --browser, for example Default or 'Profile 1'.",
    )
    parser.add_argument(
        "--cookies-file",
        help="Use an exported Netscape cookies file instead of browser cookies.",
    )
    parser.add_argument(
        "--no-browser-cookies",
        action="store_true",
        help="Do not auto-load cookies from a browser. Usually this will fail on Douyin.",
    )
    parser.add_argument(
        "--archive-file",
        default=".douyin-download-archive.txt",
        help="yt-dlp archive file path. Defaults to ./.douyin-download-archive.txt.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for resolving share links. Defaults to 30.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary staging directory after the download completes.",
    )
    return parser.parse_args()


def extract_candidate_url(text: str) -> str:
    match = DOUYIN_HOST_RE.search(text or "")
    candidate = match.group(0) if match else None
    if not candidate:
        stripped = (text or "").strip()
        if not stripped:
            raise ValueError("Douyin share text or URL cannot be empty.")
        candidate = stripped
    candidate = candidate.strip(TRIM_CHARS)
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    return candidate


def resolve_share_url(url: str, timeout: int) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()

    parsed = urlparse(final_url)
    modal_id = parse_qs(parsed.query).get("modal_id", [None])[0]
    if modal_id and modal_id.isdigit():
        return f"https://www.douyin.com/video/{modal_id}"
    return final_url


def canonicalize_douyin_url(url: str) -> str:
    parsed = urlparse(url)
    match = VIDEO_ID_RE.search(parsed.path)
    if match:
        return f"https://www.douyin.com/video/{match.group('id')}"
    match = NOTE_ID_RE.search(parsed.path)
    if match:
        return f"https://www.douyin.com/note/{match.group('id')}"
    return url


def output_folder_name(url: str) -> str:
    match = VIDEO_ID_RE.search(urlparse(url).path)
    if match:
        return f"douyin_video_{match.group('id')}"
    match = NOTE_ID_RE.search(urlparse(url).path)
    if match:
        return f"douyin_note_{match.group('id')}"
    return "douyin_media"


def find_yt_dlp_binary() -> str:
    candidates = [
        Path(sys.executable).resolve().parent / "yt-dlp",
        shutil.which("yt-dlp"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
    raise FileNotFoundError(
        "yt-dlp not found. Install it into the active environment or add it to PATH."
    )


def auto_detect_browser() -> str | None:
    checks = [
        ("chrome", Path("/Applications/Google Chrome.app")),
        ("safari", Path("/Applications/Safari.app")),
        ("edge", Path("/Applications/Microsoft Edge.app")),
        ("brave", Path("/Applications/Brave Browser.app")),
        ("firefox", Path("/Applications/Firefox.app")),
    ]
    for browser, app_path in checks:
        if app_path.exists():
            return browser
    return None


def cookie_args(args: argparse.Namespace) -> tuple[list[str], str | None]:
    if args.cookies_file:
        return ["--cookies", str(Path(args.cookies_file).expanduser().resolve())], None

    if args.no_browser_cookies:
        return [], None

    browser = args.browser or auto_detect_browser()
    if not browser:
        raise RuntimeError(
            "Douyin now usually requires fresh cookies, but no browser was provided "
            "and no supported browser could be auto-detected."
        )

    browser_spec = browser
    if args.browser_profile:
        browser_spec = f"{browser_spec}:{args.browser_profile}"
    return ["--cookies-from-browser", browser_spec], browser_spec


def classify_destination(path: Path, output_dir: Path) -> Path:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return output_dir / PHOTO_DIR_NAME
    if suffix in VIDEO_EXTENSIONS:
        return output_dir / VIDEO_DIR_NAME
    if suffix in AUDIO_EXTENSIONS:
        return output_dir / AUDIO_DIR_NAME
    return output_dir / OTHER_DIR_NAME


def move_downloads(downloaded_files: list[Path], output_dir: Path) -> dict[str, int]:
    stats = {"images": 0, "videos": 0, "audio": 0, "other": 0}
    seen: set[Path] = set()

    for path in downloaded_files:
        resolved = path.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)

        destination_dir = classify_destination(resolved, output_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / resolved.name
        shutil.move(str(resolved), str(destination))

        suffix = destination.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            stats["images"] += 1
        elif suffix in VIDEO_EXTENSIONS:
            stats["videos"] += 1
        elif suffix in AUDIO_EXTENSIONS:
            stats["audio"] += 1
        else:
            stats["other"] += 1

    return stats


def read_downloaded_file_list(list_file: Path) -> list[Path]:
    if not list_file.exists():
        return []
    return [Path(line.strip()) for line in list_file.read_text().splitlines() if line.strip()]


def scan_staging_dir(staging_dir: Path, downloaded_list_file: Path) -> list[Path]:
    return [
        path
        for path in staging_dir.rglob("*")
        if path.is_file() and path != downloaded_list_file and not path.name.endswith(".part")
    ]


def build_command(
    yt_dlp: str,
    url: str,
    staging_dir: Path,
    downloaded_list_file: Path,
    archive_file: Path,
    cookie_options: list[str],
) -> list[str]:
    return [
        yt_dlp,
        "--ignore-config",
        "--newline",
        "--continue",
        "--no-part",
        "--restrict-filenames",
        "--add-header",
        "Referer:https://www.douyin.com/",
        "--download-archive",
        str(archive_file),
        "--output",
        str(staging_dir / "%(id)s_%(autonumber)05d.%(ext)s"),
        "--print-to-file",
        "after_move:filepath",
        str(downloaded_list_file),
        *cookie_options,
        url,
    ]


def main() -> int:
    args = parse_args()

    try:
        raw_url = extract_candidate_url(args.share_text_or_url)
        resolved_url = resolve_share_url(raw_url, args.request_timeout)
        url = canonicalize_douyin_url(resolved_url)
        output_dir = allocate_output_dir(
            "douyin",
            args.output_dir,
            output_folder_name(url),
            default_name="douyin_media",
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        yt_dlp = find_yt_dlp_binary()
        cookie_options, cookie_source = cookie_args(args)
    except (ValueError, FileNotFoundError, RuntimeError, URLError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    archive_file = Path(args.archive_file).expanduser()
    if args.archive_file == ".douyin-download-archive.txt":
        archive_file = output_dir / archive_file.name
    archive_file = archive_file.resolve()
    archive_file.parent.mkdir(parents=True, exist_ok=True)

    staging_dir = output_dir / ".douyin_tmp"
    staging_dir.mkdir(parents=True, exist_ok=True)
    downloaded_list_file = staging_dir / "downloaded_paths.txt"
    if downloaded_list_file.exists():
        downloaded_list_file.unlink()

    command = build_command(
        yt_dlp=yt_dlp,
        url=url,
        staging_dir=staging_dir,
        downloaded_list_file=downloaded_list_file,
        archive_file=archive_file,
        cookie_options=cookie_options,
    )

    print(f"Resolved URL: {url}", flush=True)
    print(f"Output directory: {output_dir}", flush=True)
    if args.cookies_file:
        print(
            f"Using cookies file: {Path(args.cookies_file).expanduser().resolve()}",
            flush=True,
        )
    elif cookie_source:
        print(f"Using browser cookies: {cookie_source}", flush=True)
        print(
            "If macOS asks for Keychain access, allow it so yt-dlp can read fresh cookies.",
            flush=True,
        )
    else:
        print("Running without cookies. Douyin usually blocks this now.", flush=True)

    print("Running:", flush=True)
    print(" ".join(command), flush=True)

    completed = subprocess.run(command)
    if completed.returncode != 0:
        print(
            "\nDouyin often requires fresh browser cookies. "
            "If this failed, rerun with --browser chrome or --cookies-file <file>.",
            file=sys.stderr,
        )
        return completed.returncode

    downloaded_files = read_downloaded_file_list(downloaded_list_file)
    if not downloaded_files:
        downloaded_files = scan_staging_dir(staging_dir, downloaded_list_file)
    stats = move_downloads(downloaded_files, output_dir)

    if not args.keep_temp:
        shutil.rmtree(staging_dir, ignore_errors=True)

    total = sum(stats.values())
    if total == 0:
        print("No new files were downloaded.")
    else:
        print(
            "Done. "
            f"Photos {stats['images']}, videos {stats['videos']}, audio {stats['audio']}, other {stats['other']}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

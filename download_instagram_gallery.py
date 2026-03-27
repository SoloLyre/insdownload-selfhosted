#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from output_layout import allocate_output_dir


DEFAULT_INCLUDE = "avatar,posts,reels,stories,highlights"
DEFAULT_FILENAME = "{media_id!S}.{extension}"


def extract_username(profile: str) -> str:
    profile = profile.strip()
    if not profile:
        raise ValueError("Instagram profile URL or username cannot be empty.")

    if "instagram.com" not in profile:
        return profile.lstrip("@").strip("/")

    parsed = urlparse(profile)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        raise ValueError("Could not find a username in the Instagram URL.")
    return segments[0].lstrip("@")


def build_browser_spec(browser: str, profile: str, domain: str) -> str:
    return f"{browser}/{domain}:{profile}"


def find_gallery_dl_binary() -> str:
    candidates = [
        Path(sys.executable).parent / "gallery-dl",
        shutil.which("gallery-dl"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
    raise FileNotFoundError(
        "gallery-dl not found. Install it into the active environment or add it to PATH."
    )


def build_command(args: argparse.Namespace, username: str, output_dir: Path) -> list[str]:
    command = [
        find_gallery_dl_binary(),
        "--cookies-from-browser",
        build_browser_spec(args.browser, args.browser_profile, args.domain),
        "-D",
        str(output_dir),
        "-f",
        args.filename,
        "-o",
        f"extractor.instagram.include={args.include}",
        "--download-archive",
        str((output_dir / ".gallery-dl-archive.txt").resolve()),
        "--write-log",
        str((output_dir / "gallery-dl.log").resolve()),
        f"https://www.instagram.com/{username}/",
    ]

    if args.simulate:
        command.insert(1, "-s")
        command.insert(1, "-v")

    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download Instagram media through gallery-dl using browser cookies. "
            "This is useful for public accounts and can also attempt stories and highlights "
            "when your browser session is accepted by Instagram."
        )
    )
    parser.add_argument(
        "profile",
        help="Instagram profile URL or username.",
    )
    parser.add_argument(
        "--browser",
        default="chrome",
        help="Browser name understood by gallery-dl. Defaults to chrome.",
    )
    parser.add_argument(
        "--browser-profile",
        default="Default",
        help="Browser profile to read cookies from. Defaults to Default.",
    )
    parser.add_argument(
        "--domain",
        default="instagram.com",
        help="Cookie domain to load. Defaults to instagram.com.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Root directory where a dated profile folder should be created. "
            "If you point this at an existing dated download folder, files are written there directly."
        ),
    )
    parser.add_argument(
        "--include",
        default=DEFAULT_INCLUDE,
        help=f"Comma-separated gallery-dl include list. Defaults to {DEFAULT_INCLUDE}.",
    )
    parser.add_argument(
        "--filename",
        default=DEFAULT_FILENAME,
        help=f"gallery-dl filename template. Defaults to {DEFAULT_FILENAME}.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run gallery-dl in simulation mode with verbose output.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        username = extract_username(args.profile)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    output_dir = allocate_output_dir(
        "ins",
        args.output_dir,
        username,
        default_name="instagram_user",
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(args, username, output_dir)
    print("Running:")
    print(" ".join(command))
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from output_layout import PHOTO_DIR_NAME, VIDEO_DIR_NAME, allocate_output_dir


APP_ID = "936619743392459"
USER_AGENT = "Mozilla/5.0"


def extract_username(profile: str) -> str:
    profile = profile.strip()
    if not profile:
        raise ValueError("Instagram profile URL or username cannot be empty.")

    if "instagram.com" not in profile:
        return profile.lstrip("@").strip("/")

    parsed = urlparse(profile)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("Could not find a username in the Instagram URL.")
    return parts[0].lstrip("@")


def infer_extension(url: str, default: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix or default


def media_basename(media: dict) -> str:
    if media.get("pk"):
        return str(media["pk"])
    media_id = str(media.get("id", ""))
    if "_" in media_id:
        media_id = media_id.split("_", 1)[0]
    return media_id or str(media.get("strong_id__", "media"))


def best_image_url(media: dict) -> str | None:
    candidates = media.get("image_versions2", {}).get("candidates", [])
    if not candidates:
        return media.get("display_url")
    best = max(
        candidates,
        key=lambda c: int(c.get("width", 0)) * int(c.get("height", 0)),
    )
    return best.get("url")


def best_video_url(media: dict) -> str | None:
    versions = media.get("video_versions", [])
    if not versions:
        return None
    best = max(
        versions,
        key=lambda v: int(v.get("width", 0)) * int(v.get("height", 0)),
    )
    return best.get("url")


def curl_bytes(url: str, referer: str) -> bytes:
    command = [
        "curl",
        "-sS",
        "-L",
        url,
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        f"Referer: {referer}",
        "-H",
        "X-Requested-With: XMLHttpRequest",
        "-H",
        f"X-IG-App-ID: {APP_ID}",
        "-H",
        "Accept: application/json",
    ]
    result = subprocess.run(command, check=True, capture_output=True)
    return result.stdout


def curl_json(url: str, referer: str) -> dict:
    return json.loads(curl_bytes(url, referer).decode("utf-8"))


def curl_download(url: str, destination: Path, referer: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "curl",
        "-sS",
        "-L",
        "--fail",
        "--retry",
        "3",
        "--retry-delay",
        "1",
        url,
        "-H",
        f"User-Agent: {USER_AGENT}",
        "-H",
        f"Referer: {referer}",
        "-o",
        str(destination),
    ]
    subprocess.run(command, check=True)


def fetch_profile(username: str) -> dict:
    referer = f"https://www.instagram.com/{username}/"
    url = (
        "https://www.instagram.com/api/v1/users/web_profile_info/?"
        + urlencode({"username": username})
    )
    return curl_json(url, referer)


def fetch_timeline_page(username: str, max_id: str | None) -> dict:
    referer = f"https://www.instagram.com/{username}/"
    params = [("count", "12")]
    if max_id:
        params.append(("max_id", max_id))
    url = (
        f"https://www.instagram.com/api/v1/feed/user/{username}/username/?"
        + urlencode(params)
    )
    return curl_json(url, referer)


def fetch_extras(username: str, user_id: str) -> dict:
    referer = f"https://www.instagram.com/{username}/"
    params = [
        ("query_id", "9957820854288654"),
        ("user_id", user_id),
        ("include_chaining", "false"),
        ("include_reel", "true"),
        ("include_suggested_users", "false"),
        ("include_logged_out_extras", "true"),
        ("include_live_status", "false"),
        ("include_highlight_reels", "true"),
    ]
    url = "https://www.instagram.com/graphql/query/?" + urlencode(params)
    return curl_json(url, referer)


def fetch_highlight(username: str, highlight_id: str) -> dict:
    referer = f"https://www.instagram.com/{username}/"
    url = (
        "https://www.instagram.com/api/v1/feed/reels_media/?"
        + urlencode({"reel_ids": f"highlight:{highlight_id}"})
    )
    return curl_json(url, referer)


def fetch_story(username: str, user_id: str) -> dict:
    referer = f"https://www.instagram.com/{username}/"
    url = (
        "https://www.instagram.com/api/v1/feed/reels_media/?"
        + urlencode({"reel_ids": user_id})
    )
    return curl_json(url, referer)


def iter_media_nodes(item: dict):
    if item.get("media_type") == 8:
        for media in item.get("carousel_media", []):
            yield media
        return
    yield item


def download_media(media: dict, image_dir: Path, video_dir: Path, referer: str) -> tuple[int, int]:
    video_url = best_video_url(media)
    if video_url:
        extension = infer_extension(video_url, ".mp4")
        target = video_dir / f"{media_basename(media)}{extension}"
        if not target.exists():
            curl_download(video_url, target, referer)
        return 0, 1

    image_url = best_image_url(media)
    if image_url:
        extension = infer_extension(image_url, ".jpg")
        target = image_dir / f"{media_basename(media)}{extension}"
        if not target.exists():
            curl_download(image_url, target, referer)
        return 1, 0

    return 0, 0


def download_avatar(user: dict, image_dir: Path, referer: str) -> int:
    avatar_url = user.get("profile_pic_url_hd") or user.get("profile_pic_url")
    if not avatar_url:
        return 0
    extension = infer_extension(avatar_url, ".jpg")
    target = image_dir / f"avatar_{user['id']}{extension}"
    if not target.exists():
        curl_download(avatar_url, target, referer)
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download public Instagram media via curl-based web endpoints."
    )
    parser.add_argument("profile", help="Instagram profile URL or username.")
    parser.add_argument(
        "--output-dir",
        help=(
            "Root directory where a dated profile folder should be created. "
            "If you point this at an existing dated download folder, files are written there directly."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        username = extract_username(args.profile)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    root = allocate_output_dir(
        "ins",
        args.output_dir,
        username,
        default_name="instagram_user",
    )
    image_dir = root / PHOTO_DIR_NAME
    video_dir = root / VIDEO_DIR_NAME
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    referer = f"https://www.instagram.com/{username}/"
    profile = fetch_profile(username)
    user = profile["data"]["user"]

    image_count = download_avatar(user, image_dir, referer)
    video_count = 0
    page_count = 0

    max_id: str | None = None
    while True:
        page = fetch_timeline_page(username, max_id)
        page_count += 1
        for item in page.get("items", []):
            for media in iter_media_nodes(item):
                images, videos = download_media(media, image_dir, video_dir, referer)
                image_count += images
                video_count += videos
        if not page.get("more_available"):
            break
        max_id = page.get("next_max_id") or page.get("profile_grid_items_cursor")
        if not max_id:
            break

    extras = fetch_extras(username, user["id"])
    highlight_edges = (
        extras.get("data", {})
        .get("user", {})
        .get("edge_highlight_reels", {})
        .get("edges", [])
    )
    highlight_count = 0
    for edge in highlight_edges:
        highlight_id = edge.get("node", {}).get("id")
        if not highlight_id:
            continue
        highlight = fetch_highlight(username, highlight_id)
        reel = highlight.get("reels", {}).get(f"highlight:{highlight_id}", {})
        for item in reel.get("items", []):
            images, videos = download_media(item, image_dir, video_dir, referer)
            image_count += images
            video_count += videos
        highlight_count += 1

    story_count = 0
    try:
        story_reels = fetch_story(username, user["id"]).get("reels", {})
        user_reel = story_reels.get(user["id"], {})
        for item in user_reel.get("items", []):
            images, videos = download_media(item, image_dir, video_dir, referer)
            image_count += images
            video_count += videos
            story_count += 1
    except subprocess.CalledProcessError:
        story_count = 0

    print(f"username={username}")
    print(f"profile_id={user['id']}")
    print(f"timeline_pages={page_count}")
    print(f"highlights={highlight_count}")
    print(f"stories={story_count}")
    print(f"images={image_count}")
    print(f"videos={video_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import chompjs
import requests
import websocket
from Crypto.Cipher import AES
from xhshow import Xhshow

from browser_worker import launch_hidden_browser, terminate_hidden_browser
from input_parsing import extract_url_from_text
from output_layout import allocate_output_dir


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
)
PROFILE_RE = re.compile(r"/user/profile/(?P<user_id>[0-9a-f]+)")
NOTE_RE = re.compile(
    r"/(?:explore|discovery/item)/(?P<note_id>[0-9a-zA-Z]+)"
)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)
NOTE_API_PATH = "/api/sns/web/v1/user_posted"
NOTE_API_URL = f"https://edith.xiaohongshu.com{NOTE_API_PATH}"
CHROME_BASE = Path.home() / "Library/Application Support/Google/Chrome"
CHROME_APP = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
TARGET_CHOICES = ("auto", "profile", "single")
PROFILE_NAME_PATTERNS = [
    re.compile(r'"nickname":"([^"]+)"'),
    re.compile(r'"nickName":"([^"]+)"'),
    re.compile(r'"display_name":"([^"]+)"'),
]
PROFILE_SHARE_TEXT_PATTERN = re.compile(r"@(?P<name>.+?)\s+在小红书")
IMAGE_SCENE_PRIORITY = [
    "ORI",
    "ORIGINAL",
    "RAW",
    "DWN",
    "DLD",
    "DFT",
    "PRV",
]
IMAGE_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "application/octet-stream": "",
}


@dataclass
class DownloadItem:
    url: str
    fallback_urls: list[str]
    kind: str
    filename_stem: str
    note_id: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Xiaohongshu profile media or a single note in the best quality currently exposed."
    )
    parser.add_argument("target_url", help="Xiaohongshu share link, profile URL, or note URL")
    parser.add_argument(
        "--chrome-profile",
        default="Default",
        help="Chrome profile directory name. Defaults to Default.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Root directory where a dated profile folder should be created. "
            "If you point this at an existing dated download folder, files are written there directly."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Number of concurrent note/media workers. Defaults to 6.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=45,
        help="HTTP timeout in seconds. Defaults to 45.",
    )
    parser.add_argument(
        "--refresh-output",
        action="store_true",
        help="Delete existing media files in the output subfolders before downloading.",
    )
    parser.add_argument(
        "--target-kind",
        choices=TARGET_CHOICES,
        default="auto",
        help="Choose whether the target is a profile or a single note. Defaults to auto.",
    )
    return parser.parse_args()


def extract_xiaohongshu_target(text: str) -> str:
    return extract_url_from_text(
        text,
        ("xhslink.com", "xiaohongshu.com"),
        field_name="Xiaohongshu target",
    )


def extract_profile_name_from_share_text(text: str) -> str | None:
    raw = (text or "").strip()
    if not raw:
        return None
    match = PROFILE_SHARE_TEXT_PATTERN.search(raw)
    if not match:
        return None
    value = html.unescape(match.group("name")).strip()
    return value or None


def chrome_cookie_db(profile: str) -> Path:
    path = CHROME_BASE / profile / "Cookies"
    if not path.exists():
        raise FileNotFoundError(f"Chrome cookie DB not found: {path}")
    return path


def chrome_safe_storage_key() -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
        text=True,
    ).strip()


def decrypt_cookie(host: str, encrypted_value: bytes, value: str, password: str) -> str:
    if value:
        return value
    if isinstance(encrypted_value, memoryview):
        encrypted_value = encrypted_value.tobytes()
    if encrypted_value.startswith(b"v10"):
        key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, dklen=16)
        decrypted = AES.new(key, AES.MODE_CBC, b" " * 16).decrypt(encrypted_value[3:])
        padding = decrypted[-1]
        if 1 <= padding <= 16:
            decrypted = decrypted[:-padding]
        prefix = hashlib.sha256(host.encode()).digest()
        if decrypted.startswith(prefix):
            decrypted = decrypted[len(prefix):]
        return decrypted.decode("utf-8", "ignore")
    return encrypted_value.decode("utf-8", "ignore")


def load_xhs_cookies(profile: str) -> dict[str, str]:
    cookie_db = chrome_cookie_db(profile)
    password = chrome_safe_storage_key()
    con = sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            select host_key, name, encrypted_value, value
            from cookies
            where host_key like '%xiaohongshu.com%'
            """
        ).fetchall()
    finally:
        con.close()

    cookies: dict[str, str] = {}
    for host, name, encrypted_value, value in rows:
        decrypted = decrypt_cookie(host, encrypted_value, value, password)
        if decrypted:
            cookies[name] = decrypted
    if not cookies:
        raise RuntimeError(f"No Xiaohongshu cookies found in Chrome profile {profile!r}")
    return cookies


def make_session(cookies: dict[str, str], referer: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.xiaohongshu.com",
            "Referer": referer,
        }
    )
    session.cookies.update(cookies)
    return session


def session_get_with_retries(
    session: requests.Session,
    url: str,
    *,
    attempts: int = 3,
    retry_delay: float = 1.0,
    **kwargs,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return session.get(url, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(retry_delay * attempt)
    if last_error is None:
        raise RuntimeError(f"Failed to GET {url}")
    raise last_error


def resolve_profile_url(session: requests.Session, url: str, timeout: int) -> str:
    response = session_get_with_retries(session, url, allow_redirects=True, timeout=timeout)
    response.raise_for_status()
    return response.url


def detect_target_kind(url: str) -> str:
    path = urlparse(url).path
    if NOTE_RE.search(path):
        return "single"
    if PROFILE_RE.search(path):
        return "profile"
    raise ValueError(f"Could not determine whether Xiaohongshu URL is a profile or note: {url}")


def profile_user_id(url: str) -> str:
    match = PROFILE_RE.search(urlparse(url).path)
    if not match:
        raise ValueError(f"Could not extract user ID from {url}")
    return match.group("user_id")


def note_id_from_url(url: str) -> str:
    match = NOTE_RE.search(urlparse(url).path)
    if not match:
        raise ValueError(f"Could not extract note ID from {url}")
    return match.group("note_id")


def extract_profile_name(page_html: str, fallback: str) -> str:
    for pattern in PROFILE_NAME_PATTERNS:
        match = pattern.search(page_html)
        if match:
            return html.unescape(match.group(1)).strip()

    match = TITLE_RE.search(page_html)
    if match:
        title = html.unescape(match.group(1)).strip()
        for suffix in ("的个人主页 - 小红书", " - 小红书", "的个人主页", " | 小红书"):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip()
        if title and title != "小红书 - 你的生活兴趣社区":
            return title

    return fallback


def chrome_binary() -> Path:
    if not CHROME_APP.exists():
        raise FileNotFoundError(f"Chrome app not found: {CHROME_APP}")
    return CHROME_APP


def allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def copy_browser_profile(profile: str) -> Path:
    source_profile = CHROME_BASE / profile
    if not source_profile.exists():
        raise FileNotFoundError(f"Chrome profile not found: {source_profile}")

    user_data_dir = Path(tempfile.mkdtemp(prefix="xhs_cdp_"))
    target_profile = user_data_dir / "Default"
    target_profile.mkdir(parents=True, exist_ok=True)

    local_state = CHROME_BASE / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, user_data_dir / "Local State")

    for name in ("Cookies", "Preferences"):
        source = source_profile / name
        if source.exists():
            shutil.copy2(source, target_profile / name)

    for name in ("Local Storage", "Session Storage", "IndexedDB"):
        source = source_profile / name
        if source.exists():
            shutil.copytree(source, target_profile / name, dirs_exist_ok=True)

    return user_data_dir


class CDPSession:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        current_id = self._id
        self.ws.send(json.dumps({"id": current_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == current_id:
                return message

    def evaluate(self, expression: str, *, await_promise: bool = False):
        response = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        result = response.get("result", {})
        if "exceptionDetails" in result:
            description = (
                result.get("result", {}).get("description")
                or result.get("result", {}).get("value")
                or "JavaScript evaluation failed"
            )
            raise RuntimeError(str(description))
        return result.get("result", {}).get("value")

    def close(self) -> None:
        self.ws.close()


def wait_for_debug_port(port: int, timeout_seconds: int = 30) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Chrome remote debugging port did not become available.")


def wait_for_target_fragment(port: int, target_fragment: str, timeout_seconds: int = 30) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.load(response)
        except Exception:
            time.sleep(0.5)
            continue

        matches = [
            target
            for target in targets
            if target.get("type") == "page" and target_fragment in target.get("url", "")
        ]
        if matches:
            return matches[0]
        time.sleep(0.5)

    raise RuntimeError(f"Could not find the Xiaohongshu browser tab over CDP for {target_fragment}.")


PROFILE_NOTE_SNAPSHOT_SCRIPT = r"""
(() => {
  const items = [];
  for (const anchor of document.querySelectorAll('a[href*="/user/profile/"]')) {
    try {
      const url = new URL(anchor.href, location.origin);
      const match = url.pathname.match(/\/user\/profile\/[^/]+\/([0-9a-zA-Z]+)/);
      if (!match) continue;
      items.push({
        note_id: match[1],
        xsec_token: url.searchParams.get("xsec_token") || "",
      });
    } catch (_) {}
  }

  const refs = [];
  const seen = new Set();
  for (const item of items) {
    if (!item.note_id || seen.has(item.note_id)) continue;
    seen.add(item.note_id);
    refs.push(item);
  }

  return {
    refs,
    title: document.title || "",
    bodyText: document.body ? (document.body.innerText || "") : "",
    scrollY: window.scrollY || 0,
    clientHeight: document.documentElement ? document.documentElement.clientHeight : 0,
    scrollHeight: document.documentElement ? document.documentElement.scrollHeight : 0,
  };
})()
"""


def profile_note_refs_from_state(state: dict) -> tuple[list[tuple[str, str]], bool]:
    refs: list[tuple[str, str]] = []
    seen = set()
    user_state = state.get("user") or {}
    for group in user_state.get("notes") or []:
        if not isinstance(group, list):
            continue
        for entry in group:
            if not isinstance(entry, dict):
                continue
            note_card = entry.get("noteCard") or {}
            note_id = (
                entry.get("id")
                or entry.get("noteId")
                or note_card.get("noteId")
            )
            xsec_token = (
                entry.get("xsecToken")
                or note_card.get("xsecToken")
                or ""
            )
            if not note_id or note_id in seen:
                continue
            seen.add(note_id)
            refs.append((note_id, xsec_token))

    note_queries = user_state.get("noteQueries") or []
    has_more = bool(note_queries and isinstance(note_queries[0], dict) and note_queries[0].get("hasMore"))
    return refs, has_more


def merge_note_refs(*ref_groups: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: dict[str, str] = {}
    for group in ref_groups:
        for note_id, xsec_token in group:
            if not note_id:
                continue
            if note_id not in merged or (not merged[note_id] and xsec_token):
                merged[note_id] = xsec_token
    return list(merged.items())


def collect_profile_note_refs_with_browser(
    profile_url: str,
    browser_profile: str,
    timeout: int,
    *,
    max_scrolls: int = 120,
    stable_rounds: int = 4,
) -> list[tuple[str, str]]:
    user_data_dir = copy_browser_profile(browser_profile)
    port = allocate_port()
    process = launch_hidden_browser(
        chrome_binary(),
        user_data_dir=user_data_dir,
        port=port,
        url=profile_url,
    )

    session: CDPSession | None = None
    try:
        wait_for_debug_port(port, timeout_seconds=max(30, timeout))
        target = wait_for_target_fragment(
            port,
            urlparse(profile_url).path,
            timeout_seconds=max(30, timeout),
        )
        session = CDPSession(target["webSocketDebuggerUrl"])
        session.call("Runtime.enable")

        snapshot = None
        for _ in range(max(10, timeout)):
            snapshot = session.evaluate(PROFILE_NOTE_SNAPSHOT_SCRIPT, await_promise=False) or {}
            if snapshot.get("title"):
                break
            time.sleep(1)
        if not snapshot:
            raise RuntimeError("Could not read Xiaohongshu profile page state from Chrome.")

        if "登录" in (snapshot.get("bodyText") or "") and not (snapshot.get("refs") or []):
            raise RuntimeError(
                "Xiaohongshu profile opened a login gate in Chrome. Open the profile in Chrome once, then retry."
            )

        discovered: dict[str, str] = {}
        stable = 0
        previous_signature: tuple[int, int, int] | None = None

        for _ in range(max_scrolls):
            snapshot = session.evaluate(PROFILE_NOTE_SNAPSHOT_SCRIPT, await_promise=False) or {}
            for item in snapshot.get("refs") or []:
                if not isinstance(item, dict):
                    continue
                note_id = item.get("note_id") or ""
                xsec_token = item.get("xsec_token") or ""
                if note_id and (note_id not in discovered or (not discovered[note_id] and xsec_token)):
                    discovered[note_id] = xsec_token

            scroll_height = int(snapshot.get("scrollHeight") or 0)
            client_height = int(snapshot.get("clientHeight") or 0)
            scroll_y = int(snapshot.get("scrollY") or 0)
            signature = (len(discovered), scroll_height, scroll_y)
            near_bottom = scroll_y + client_height >= max(0, scroll_height - 24)

            if signature == previous_signature and near_bottom:
                stable += 1
            else:
                stable = 0
            if stable >= stable_rounds:
                break

            previous_signature = signature
            session.evaluate(
                """
                (() => {
                  const step = Math.max(window.innerHeight || 0, 900);
                  window.scrollTo(0, Math.min(
                    (window.scrollY || 0) + step,
                    document.documentElement ? document.documentElement.scrollHeight : step
                  ));
                })()
                """,
                await_promise=False,
            )
            time.sleep(1.5)

        return list(discovered.items())
    finally:
        if session is not None:
            session.close()
        terminate_hidden_browser(process)
        shutil.rmtree(user_data_dir, ignore_errors=True)


def signed_note_list_request(
    session: requests.Session,
    signer: Xhshow,
    cookies: dict[str, str],
    profile_url: str,
    user_id: str,
    cursor: str,
    timeout: int,
) -> dict:
    params = {"num": "30", "cursor": cursor, "user_id": user_id}
    signed_headers = signer.sign_headers_get(NOTE_API_PATH, cookies, params=params)
    response = session_get_with_retries(
        session,
        NOTE_API_URL,
        headers={**session.headers, **signed_headers, "Referer": profile_url},
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"List API returned an error payload: {payload}")
    return payload["data"]


def list_all_notes(
    session: requests.Session,
    signer: Xhshow,
    cookies: dict[str, str],
    profile_url: str,
    user_id: str,
    timeout: int,
) -> list[dict]:
    all_notes: list[dict] = []
    seen_cursors = set()
    cursor = ""
    while True:
        data = signed_note_list_request(
            session=session,
            signer=signer,
            cookies=cookies,
            profile_url=profile_url,
            user_id=user_id,
            cursor=cursor,
            timeout=timeout,
        )
        notes = data.get("notes") or []
        if not notes:
            break
        all_notes.extend(notes)
        next_cursor = data.get("cursor") or ""
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return all_notes


def note_url(note_id: str, xsec_token: str) -> str:
    return (
        f"https://www.xiaohongshu.com/explore/{note_id}"
        f"?xsec_token={xsec_token}&xsec_source=pc_user"
    )


def parse_note_state(html: str) -> dict:
    match = STATE_RE.search(html)
    if match:
        return chompjs.parse_js_object(match.group(1))

    marker = "window.__INITIAL_STATE__"
    marker_index = html.find(marker)
    if marker_index < 0:
        raise RuntimeError("INITIAL_STATE not found in note page")

    start = html.find("{", marker_index)
    if start < 0:
        raise RuntimeError("INITIAL_STATE object start not found in note page")

    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(html)):
        char = html[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue

        if char in ('"', "'", "`"):
            quote = char
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return chompjs.parse_js_object(html[start : index + 1])

    raise RuntimeError("INITIAL_STATE object end not found in note page")


def note_from_state(note_id: str, state: dict) -> dict:
    note_detail_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    note = (note_detail_map.get(note_id) or {}).get("note")
    if not note:
        raise RuntimeError("Note payload missing from INITIAL_STATE")
    return note


def normalize_url(url: str) -> str:
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def best_stream_url(streams: dict | None) -> str | None:
    if not streams:
        return None
    candidates = []
    for codec in ("av1", "h266", "h265", "h264"):
        for item in streams.get(codec) or []:
            url = normalize_url(item.get("masterUrl") or "")
            if not url:
                continue
            stream_desc = (item.get("streamDesc") or "").upper()
            has_watermark = 1 if "WM_" in stream_desc or "WATERMARK" in stream_desc else 0
            size = item.get("size") or 0
            bitrate = item.get("avgBitrate") or item.get("videoBitrate") or 0
            width = item.get("width") or 0
            height = item.get("height") or 0
            codec_rank = {"av1": 4, "h266": 3, "h265": 2, "h264": 1}[codec]
            # Prefer non-watermarked streams even if their bitrate is lower.
            candidates.append(((1 - has_watermark, width * height, bitrate, size, codec_rank), url))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def best_image_url(image_info: dict) -> str | None:
    by_scene = {}
    for info in image_info.get("infoList") or []:
        url = normalize_url(info.get("url") or "")
        scene = (info.get("imageScene") or "").upper()
        if url:
            by_scene[scene] = url
    for needle in IMAGE_SCENE_PRIORITY:
        for scene, url in by_scene.items():
            if needle in scene:
                return url
    for key in ("url", "urlDefault", "urlPre"):
        url = normalize_url(image_info.get(key) or "")
        if url:
            return url
    return None


def image_token(image_url: str) -> str | None:
    parsed = urlparse(normalize_url(image_url))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3:
        return None
    token = "/".join(parts[2:])
    if "!" in token:
        token = token.split("!", 1)[0]
    return token or None


def best_image_urls(image_info: dict) -> list[str]:
    candidates: list[str] = []
    display_url = best_image_url(image_info)
    source_urls = []
    for info in image_info.get("infoList") or []:
        source_urls.append(info.get("url") or "")
    for key in ("url", "urlDefault", "urlPre"):
        source_urls.append(image_info.get(key) or "")

    for source_url in source_urls:
        token = image_token(source_url)
        if not token:
            continue
        # Prefer full-resolution assets first. HEIC preserves more of the original upload when available;
        # AUTO falls back to the server's native format and often returns a larger image than page previews.
        candidates.append(f"https://ci.xiaohongshu.com/{token}?imageView2/format/heic")
        candidates.append(f"https://sns-img-bd.xhscdn.com/{token}")
        candidates.append(f"https://ci.xiaohongshu.com/{token}?imageView2/format/jpeg")
        candidates.append(f"https://ci.xiaohongshu.com/{token}?imageView2/format/png")
        candidates.append(f"https://ci.xiaohongshu.com/{token}?imageView2/format/webp")
        break

    if display_url:
        candidates.append(display_url)

    deduped: list[str] = []
    seen = set()
    for url in candidates:
        normalized = normalize_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def extract_download_items(note_id: str, note: dict) -> list[DownloadItem]:
    items: list[DownloadItem] = []

    video_url = best_stream_url(((note.get("video") or {}).get("media") or {}).get("stream"))
    if video_url:
        items.append(DownloadItem(video_url, [], "video", note_id, note_id))

    for index, image in enumerate(note.get("imageList") or [], start=1):
        image_urls = best_image_urls(image)
        if image_urls:
            items.append(
                DownloadItem(
                    image_urls[0],
                    image_urls[1:],
                    "image",
                    f"{note_id}_{index:02d}",
                    note_id,
                )
            )
        live_url = best_stream_url(image.get("stream"))
        if live_url:
            items.append(
                DownloadItem(live_url, [], "video", f"{note_id}_{index:02d}_live", note_id)
            )

    deduped: list[DownloadItem] = []
    seen = set()
    for item in items:
        key = (item.kind, item.filename_stem, item.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def note_author_name(note: dict, fallback: str) -> str:
    user = note.get("user") or {}
    for key in ("nickname", "nickName", "name"):
        value = (user.get(key) or "").strip()
        if value:
            return value
    author = note.get("author") or {}
    for key in ("nickname", "nickName", "name"):
        value = (author.get(key) or "").strip()
        if value:
            return value
    return fallback


def extension_for_response(url: str, response: requests.Response) -> str:
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type in IMAGE_EXT_BY_TYPE and IMAGE_EXT_BY_TYPE[content_type]:
        return IMAGE_EXT_BY_TYPE[content_type]
    parsed = Path(urlparse(url).path)
    if parsed.suffix:
        return parsed.suffix.lower()
    guess = mimetypes.guess_extension(content_type)
    if guess:
        return ".jpg" if guess == ".jpe" else guess
    return ".bin"


def extension_for_bytes(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return ".heic"
        if brand in {b"avif", b"avis"}:
            return ".avif"
        if brand in {
            b"isom",
            b"iso2",
            b"iso3",
            b"iso4",
            b"iso5",
            b"iso6",
            b"mp41",
            b"mp42",
            b"avc1",
            b"dash",
            b"M4V ",
            b"MSNV",
            b"3gp4",
        }:
            return ".mp4"
    return None


def safe_write_path(directory: Path, stem: str, suffix: str) -> Path:
    path = directory / f"{stem}{suffix}"
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def fetch_note_items(
    session: requests.Session,
    note_id: str,
    xsec_token: str,
    timeout: int,
) -> list[DownloadItem]:
    target_url = note_url(note_id, xsec_token)
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.xiaohongshu.com/"}

    last_error: Exception | None = None
    for current_session in (
        session,
        requests.Session(),
    ):
        if current_session is not session:
            current_session.headers.update(headers)

        response = None
        try:
            response = session_get_with_retries(
                current_session,
                target_url,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            if "/website-login/error" in response.url:
                raise RuntimeError(f"Note page redirected to login error: {response.url}")
            state = parse_note_state(response.text)
            note = note_from_state(note_id, state)
            return extract_download_items(note_id, note)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        finally:
            if response is not None:
                response.close()
            if current_session is not session:
                current_session.close()

    if last_error is None:
        raise RuntimeError("Failed to fetch note items")
    raise last_error


def fetch_note_items_from_url(
    session: requests.Session,
    target_url: str,
    note_id: str,
    timeout: int,
) -> tuple[list[DownloadItem], dict, str]:
    response = session_get_with_retries(
        session,
        target_url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://www.xiaohongshu.com/"},
        timeout=timeout,
    )
    try:
        response.raise_for_status()
        if "/website-login/error" in response.url:
            raise RuntimeError(f"Note page redirected to login error: {response.url}")
        state = parse_note_state(response.text)
        note = note_from_state(note_id, state)
        return extract_download_items(note_id, note), note, response.url
    finally:
        response.close()


def download_binary(
    session: requests.Session,
    item: DownloadItem,
    image_dir: Path,
    video_dir: Path,
    timeout: int,
) -> Path:
    directory = image_dir if item.kind == "image" else video_dir
    last_error: Exception | None = None
    candidate_urls = [item.url, *item.fallback_urls]
    for candidate_url in candidate_urls:
        response = None
        temp_destination = None
        try:
            response = session_get_with_retries(
                session,
                candidate_url,
                stream=True,
                timeout=timeout,
            )
            response.raise_for_status()
            temp_destination = directory / f".{item.filename_stem}.{uuid.uuid4().hex}.part"
            sniff = b""
            with temp_destination.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        if len(sniff) < 64:
                            sniff = (sniff + chunk)[:64]
                        handle.write(chunk)
            suffix = extension_for_bytes(sniff) or extension_for_response(candidate_url, response)
            destination = safe_write_path(directory, item.filename_stem, suffix)
            temp_destination.replace(destination)
            return destination
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        finally:
            if response is not None:
                response.close()
            if temp_destination is not None and temp_destination.exists():
                temp_destination.unlink()
    if last_error is None:
        raise RuntimeError("No download URLs available")
    raise last_error


def load_existing_stems(image_dir: Path, video_dir: Path) -> set[str]:
    stems = set()
    for directory in (image_dir, video_dir):
        for path in directory.iterdir():
            if path.is_file():
                stems.add(path.stem)
    return stems


def clear_output_dir(output_dir: Path) -> None:
    for path in sorted(output_dir.iterdir(), reverse=True):
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
        else:
            path.unlink()


def download_single_note(
    cookies: dict[str, str],
    target_url: str,
    note_id: str,
    output_root: str | None,
    timeout: int,
    refresh_output: bool,
) -> int:
    session = make_session(cookies, target_url)
    items, note, final_note_url = fetch_note_items_from_url(session, target_url, note_id, timeout)
    author_name = note_author_name(note, note_id)
    output_dir = allocate_output_dir(
        "xiaohongshu",
        output_root,
        author_name,
        default_name="xiaohongshu_user",
    )
    image_dir = output_dir / "图片"
    video_dir = output_dir / "视频"
    if refresh_output:
        output_dir.mkdir(parents=True, exist_ok=True)
        clear_output_dir(output_dir)
    manifest_path = output_dir / "download_manifest.json"
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    print(f"Resolved note URL: {final_note_url}")
    print(f"Note ID: {note_id}")
    print(f"Output directory: {output_dir}")

    existing_stems = load_existing_stems(image_dir, video_dir)
    stats = {
        "notes_total": 1,
        "notes_ok": 1,
        "notes_failed": 0,
        "images_downloaded": 0,
        "videos_downloaded": 0,
        "skipped_existing": 0,
        "media_failed": 0,
    }
    failures: list[dict] = []
    start = time.time()

    for item in items:
        if item.filename_stem in existing_stems:
            stats["skipped_existing"] += 1
            continue
        try:
            path = download_binary(
                session=session,
                item=item,
                image_dir=image_dir,
                video_dir=video_dir,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            stats["media_failed"] += 1
            failures.append(
                {
                    "scope": "media",
                    "note_id": note_id,
                    "filename_stem": item.filename_stem,
                    "url": item.url,
                    "error": str(exc),
                }
            )
            continue
        existing_stems.add(path.stem)
        if item.kind == "image":
            stats["images_downloaded"] += 1
        else:
            stats["videos_downloaded"] += 1

    if failures:
        stats["notes_ok"] = 0
        stats["notes_failed"] = 1

    manifest = {
        "target_kind": "single",
        "note_url": final_note_url,
        "note_id": note_id,
        "author_name": author_name,
        "note_title": (note.get("title") or "").strip(),
        "stats": stats,
        "elapsed_seconds": round(time.time() - start, 2),
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": int(time.time()),
        "failures": failures,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["stats"], ensure_ascii=False, indent=2))
    print(f"Manifest written to: {manifest_path}")
    return 0 if not failures else 1


def main() -> int:
    args = parse_args()

    share_profile_name = extract_profile_name_from_share_text(args.target_url)
    cookies = load_xhs_cookies(args.chrome_profile)
    bootstrap_session = make_session(cookies, "https://www.xiaohongshu.com/")
    normalized_target_url = extract_xiaohongshu_target(args.target_url)
    final_target_url = resolve_profile_url(bootstrap_session, normalized_target_url, args.request_timeout)
    target_kind = args.target_kind
    if target_kind == "auto":
        target_kind = detect_target_kind(final_target_url)

    if target_kind == "single":
        note_id = note_id_from_url(final_target_url)
        return download_single_note(
            cookies=cookies,
            target_url=final_target_url,
            note_id=note_id,
            output_root=args.output_dir,
            timeout=args.request_timeout,
            refresh_output=args.refresh_output,
        )

    user_id = profile_user_id(final_target_url)
    profile_page = session_get_with_retries(
        bootstrap_session,
        final_target_url,
        allow_redirects=True,
        timeout=args.request_timeout,
    )
    profile_name = share_profile_name or extract_profile_name(profile_page.text, user_id)
    profile_state = parse_note_state(profile_page.text)
    output_dir = allocate_output_dir(
        "xiaohongshu",
        args.output_dir,
        profile_name,
        default_name="xiaohongshu_user",
    )
    image_dir = output_dir / "图片"
    video_dir = output_dir / "视频"
    if args.refresh_output:
        output_dir.mkdir(parents=True, exist_ok=True)
        clear_output_dir(output_dir)
    manifest_path = output_dir / "download_manifest.json"
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    print(f"Resolved profile URL: {final_target_url}")
    print(f"User ID: {user_id}")
    print(f"Profile name: {profile_name}")
    print(f"Output directory: {output_dir}")

    initial_note_refs, has_more = profile_note_refs_from_state(profile_state)
    if has_more:
        browser_note_refs = collect_profile_note_refs_with_browser(
            final_target_url,
            args.chrome_profile,
            args.request_timeout,
        )
        note_refs = merge_note_refs(initial_note_refs, browser_note_refs)
    else:
        note_refs = initial_note_refs
    print(f"Discovered notes: {len(note_refs)}")

    existing_stems = load_existing_stems(image_dir, video_dir)
    lock = threading.Lock()
    stats = {
        "notes_total": len(note_refs),
        "notes_ok": 0,
        "notes_failed": 0,
        "images_downloaded": 0,
        "videos_downloaded": 0,
        "skipped_existing": 0,
        "media_failed": 0,
    }
    failures: list[dict] = []

    def worker(note_ref: tuple[str, str]) -> dict:
        note_id, xsec_token = note_ref
        session = make_session(cookies, final_target_url)
        result = {
            "note_id": note_id,
            "images": 0,
            "videos": 0,
            "skipped": 0,
            "media_failed": 0,
            "error": None,
        }
        try:
            items = fetch_note_items(session, note_id, xsec_token, args.request_timeout)
            for item in items:
                with lock:
                    if item.filename_stem in existing_stems:
                        result["skipped"] += 1
                        continue
                try:
                    path = download_binary(
                        session=session,
                        item=item,
                        image_dir=image_dir,
                        video_dir=video_dir,
                        timeout=args.request_timeout,
                    )
                except Exception as exc:  # noqa: BLE001
                    result["media_failed"] += 1
                    failures.append(
                        {
                            "scope": "media",
                            "note_id": note_id,
                            "filename_stem": item.filename_stem,
                            "url": item.url,
                            "error": str(exc),
                        }
                    )
                    continue
                with lock:
                    existing_stems.add(path.stem)
                if item.kind == "image":
                    result["images"] += 1
                else:
                    result["videos"] += 1
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
        return result

    start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        future_map = {executor.submit(worker, note_ref): note_ref[0] for note_ref in note_refs}
        for index, future in enumerate(as_completed(future_map), start=1):
            result = future.result()
            note_id = result["note_id"]
            if result["error"]:
                stats["notes_failed"] += 1
                failures.append({"scope": "note", "note_id": note_id, "error": result["error"]})
                print(f"[{index}/{len(note_refs)}] note {note_id} failed: {result['error']}")
                continue
            stats["notes_ok"] += 1
            stats["images_downloaded"] += result["images"]
            stats["videos_downloaded"] += result["videos"]
            stats["skipped_existing"] += result["skipped"]
            stats["media_failed"] += result["media_failed"]
            print(
                f"[{index}/{len(note_refs)}] note {note_id}: "
                f"+{result['images']} image(s), +{result['videos']} video(s), "
                f"{result['skipped']} skipped, {result['media_failed']} failed"
            )

    manifest = {
        "target_kind": "profile",
        "profile_url": final_target_url,
        "user_id": user_id,
        "profile_name": profile_name,
        "stats": stats,
        "elapsed_seconds": round(time.time() - start, 2),
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_at": int(time.time()),
        "failures": failures,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(manifest["stats"], ensure_ascii=False, indent=2))
    print(f"Manifest written to: {manifest_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

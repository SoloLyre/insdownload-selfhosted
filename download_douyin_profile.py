#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from yt_dlp.cookies import extract_cookies_from_browser
import websocket

from browser_worker import HiddenBrowserHandle, launch_hidden_browser, terminate_hidden_browser
from download_douyin import (
    SUPPORTED_BROWSERS,
    USER_AGENT,
    canonicalize_douyin_url,
    extract_candidate_url,
    resolve_share_url,
)
from output_layout import allocate_output_dir


CHROME_APP_PATHS = {
    "chrome": Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "brave": Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    "edge": Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    "chromium": Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
}
VIDEO_ID_RE = re.compile(r"/video/(?P<id>\d+)")
TARGET_CHOICES = ("auto", "profile", "single")
EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "application/octet-stream": "",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download a Douyin profile or single post using the highest-quality media candidates "
            "currently exposed by the web payload."
        )
    )
    parser.add_argument(
        "share_text_or_url",
        help="Douyin profile share text/link or single-post link.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Root directory where a dated nickname folder should be created. "
            "If you point this at an existing dated download folder, files are written there directly."
        ),
    )
    parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        default="chrome",
        help="Browser to read Douyin cookies from. Defaults to chrome.",
    )
    parser.add_argument(
        "--browser-profile",
        default="Default",
        help="Browser profile directory name. Defaults to Default.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=30,
        help="HTTP timeout in seconds. Defaults to 30.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=18,
        help="How many posts to fetch per page. Defaults to 18.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Optional page limit for debugging.",
    )
    parser.add_argument(
        "--target-kind",
        choices=TARGET_CHOICES,
        default="auto",
        help="Choose whether the target is a profile or a single post. Defaults to auto.",
    )
    return parser.parse_args()

def chrome_binary(browser: str) -> Path:
    path = CHROME_APP_PATHS.get(browser)
    if not path or not path.exists():
        raise FileNotFoundError(
            f"Unsupported or missing browser app for {browser!r}. "
            "Use chrome, brave, edge, or chromium."
        )
    return path


def extract_sec_uid(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    sec_uid = query.get("sec_uid", [None])[0]
    if sec_uid:
        return sec_uid
    parts = [segment for segment in parsed.path.split("/") if segment]
    if len(parts) >= 3 and parts[-2] == "user":
        return parts[-1]
    raise ValueError(f"Could not extract sec_uid from {url}")


def extract_aweme_id(url: str) -> str:
    parsed = urlparse(url)
    modal_id = parse_qs(parsed.query).get("modal_id", [None])[0]
    if modal_id and modal_id.isdigit():
        return modal_id
    match = VIDEO_ID_RE.search(parsed.path)
    if match:
        return match.group("id")
    raise ValueError(f"Could not extract aweme_id from {url}")


def detect_target_kind(url: str) -> str:
    if VIDEO_ID_RE.search(urlparse(url).path):
        return "single"
    return "profile"


def allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def copy_browser_profile(browser_profile: str) -> Path:
    chrome_root = Path.home() / "Library/Application Support/Google/Chrome"
    source_profile = chrome_root / browser_profile
    if not source_profile.exists():
        raise FileNotFoundError(f"Chrome profile not found: {source_profile}")

    user_data_dir = Path(tempfile.mkdtemp(prefix="douyin_cdp_"))
    target_profile = user_data_dir / "Default"
    target_profile.mkdir(parents=True, exist_ok=True)

    local_state = chrome_root / "Local State"
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
        self.ws = websocket.create_connection(ws_url, timeout=20)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        current = self._id
        self.ws.send(json.dumps({"id": current, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == current:
                return message

    def evaluate(self, expression: str, *, await_promise: bool = True):
        response = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if "exceptionDetails" in response.get("result", {}):
            exception_details = response["result"].get("exceptionDetails") or {}
            exception_obj = exception_details.get("exception") or {}
            description = (
                exception_obj.get("description")
                or exception_details.get("text")
                or response["result"]["result"].get("description")
                or response["result"]["result"].get("value")
                or "JavaScript evaluation failed"
            )
            raise RuntimeError(str(description))
        return response["result"]["result"].get("value")

    def close(self) -> None:
        self.ws.close()


def wait_for_target_fragment(port: int, target_fragment: str, timeout_seconds: int = 30) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.load(response)
            matches = [
                target
                for target in targets
                if target.get("type") == "page" and target_fragment in target.get("url", "")
            ]
            if matches:
                return matches[0]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Could not find the Douyin browser tab over CDP for {target_fragment}.")


def wait_for_debug_port(port: int, timeout_seconds: int = 30) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Chrome remote debugging port did not become available.")


def page_title(session: CDPSession) -> str:
    title = session.evaluate("document.title", await_promise=False)
    return title or ""


def wait_for_video_page_ready(session: CDPSession, aweme_id: str, timeout_seconds: int = 30) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        href = session.evaluate("location.href", await_promise=False) or ""
        ready_state = session.evaluate("document.readyState", await_promise=False) or ""
        title = page_title(session)
        if aweme_id in href and ready_state in {"interactive", "complete"}:
            if title and "验证码" not in title:
                return
        time.sleep(1)


def discover_detail_request_template(session: CDPSession, aweme_id: str) -> str | None:
    script = f"""
    (() => {{
      return performance.getEntriesByType("resource")
        .map(entry => entry.name)
        .find(url => url.includes("/aweme/v1/web/aweme/detail/?") && url.includes({json.dumps(aweme_id)}))
        || null;
    }})()
    """
    value = session.evaluate(script, await_promise=False)
    return value or None


def build_resource_fetch_expression(resource_url: str) -> str:
    return f"""
    (async () => {{
      let lastText = "";
      for (let attempt = 0; attempt < 8; attempt++) {{
        const response = await fetch({json.dumps(resource_url)}, {{credentials: "include"}});
        const text = await response.text();
        lastText = text;
        if (text && text.trim() && text.trim() != "{{}}") {{
          return text;
        }}
        await new Promise(resolve => setTimeout(resolve, 1200));
      }}
      throw new Error("Empty Douyin resource response after retries; last length=" + lastText.length);
    }})()
    """


def profile_page_meta(session: CDPSession) -> dict:
    script = """
    (async () => {
      for (let i = 0; i < 5; i++) {
        window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      const text = document.body ? document.body.innerText : "";
      const match = text.match(/作品\\s*(\\d+)/);
      return {
        title: document.title || "",
        declared_post_count: match ? Number(match[1]) : null,
        login_gate_visible: text.includes("登录后查看更多作品"),
        logged_in_ui_visible: !text.includes("\\n登录\\n") && !text.includes("立即登录"),
      };
    })()
    """
    return session.evaluate(script, await_promise=True) or {}


def discover_post_request_template(session: CDPSession) -> str | None:
    script = """
    (async () => {
      for (let i = 0; i < 12; i++) {
        const found = performance.getEntriesByType("resource")
          .map(entry => entry.name)
          .find(url => url.includes("/aweme/v1/web/aweme/post/?"));
        if (found) {
          return found;
        }
        window.scrollTo(0, document.body ? document.body.scrollHeight : 0);
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      return null;
    })()
    """
    value = session.evaluate(script, await_promise=True)
    return value or None


def build_page_fetch_expression(sec_uid: str, cursor: str, page_size: int) -> str:
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "sec_user_id": sec_uid,
        "max_cursor": cursor,
        "count": str(page_size),
        "version_code": "170400",
        "version_name": "17.4.0",
    }
    url = "https://www.douyin.com/aweme/v1/web/aweme/post/?" + urlencode(params)
    return f"""
    (async () => {{
      let lastText = "";
      for (let attempt = 0; attempt < 8; attempt++) {{
        const response = await fetch({json.dumps(url)}, {{credentials: "include"}});
        const text = await response.text();
        lastText = text;
        if (text && text.trim()) {{
          return text;
        }}
        await new Promise(resolve => setTimeout(resolve, 1500));
      }}
      throw new Error("Empty Douyin response after retries; last length=" + lastText.length);
    }})()
    """


def build_signed_page_fetch_expression(resource_url: str, cursor: str, page_size: int) -> str:
    return f"""
    (async () => {{
      let lastText = "";
      for (let attempt = 0; attempt < 8; attempt++) {{
        const url = new URL({json.dumps(resource_url)});
        url.searchParams.set("max_cursor", {json.dumps(cursor)});
        url.searchParams.set("count", {json.dumps(str(page_size))});
        url.searchParams.delete("a_bogus");

        const signed = window.byted_acrawler?.frontierSign
          ? window.byted_acrawler.frontierSign(url.toString())
          : null;
        const bogus = signed && (signed["X-Bogus"] || signed["x-bogus"] || signed["a_bogus"]);
        if (bogus) {{
          url.searchParams.set("a_bogus", bogus);
        }}

        const response = await fetch(url.toString(), {{credentials: "include"}});
        const text = await response.text();
        lastText = text;
        if (text && text.trim()) {{
          return text;
        }}
        await new Promise(resolve => setTimeout(resolve, 1500));
      }}
      throw new Error("Empty Douyin response after retries; last length=" + lastText.length);
    }})()
    """


def build_detail_fetch_expression(aweme_id: str) -> str:
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": aweme_id,
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
    }
    url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?" + urlencode(params)
    return f"""
    (async () => {{
      let lastText = "";
      for (let attempt = 0; attempt < 8; attempt++) {{
        const target = new URL({json.dumps(url)});
        const signed = window.byted_acrawler?.frontierSign
          ? window.byted_acrawler.frontierSign(target.toString())
          : null;
        const bogus = typeof signed === "string"
          ? signed
          : signed && (signed["X-Bogus"] || signed["x-bogus"] || signed["a_bogus"]);
        if (bogus) {{
          target.searchParams.set("a_bogus", bogus);
        }}

        const response = await fetch(target.toString(), {{credentials: "include"}});
        const text = await response.text();
        lastText = text;
        if (text && text.trim() && text.trim() != "{{}}") {{
          return text;
        }}
        await new Promise(resolve => setTimeout(resolve, 1500));
      }}
      throw new Error("Empty Douyin detail response after retries; last length=" + lastText.length);
    }})()
    """


def extract_single_post_from_dom(session: CDPSession, aweme_id: str) -> dict | None:
    script = """
    (() => {
      const decodeHtml = (value) => {
        const textarea = document.createElement("textarea");
        textarea.innerHTML = value || "";
        return textarea.value;
      };
      const getMeta = (selector) => document.querySelector(selector)?.getAttribute("content") || "";
      const title = (document.title || "").replace(/\\s*-\\s*抖音$/, "").trim();
      const description = getMeta('meta[name="description"]');
      let author = "";
      for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
          const data = JSON.parse(script.textContent || "{}");
          const items = data.itemListElement || [];
          const match = items.find(item => item && item.position === 2);
          if (match && match.name) {
            author = String(match.name).trim();
            break;
          }
        } catch (error) {}
      }
      if (!author && description) {
        const match = description.match(/-\\s*(.+?)于\\d{8}发布在抖音/);
        if (match) {
          author = match[1].trim();
        }
      }
      const sourceUrls = [];
      for (const node of document.querySelectorAll("video source[src], video[src]")) {
        const raw = node.currentSrc || node.src || "";
        if (!raw) continue;
        sourceUrls.push(decodeHtml(raw));
      }
      const imageMap = new Map();
      for (const img of document.querySelectorAll("img[src]")) {
        const raw = img.currentSrc || img.src || "";
        const url = decodeHtml(raw);
        if (!url || !/douyinpic\\.com|byteimg\\.com/i.test(url)) continue;
        if (/avatar|favicon|logo|aweme-avatar/i.test(url)) continue;
        const width = img.naturalWidth || img.width || 0;
        const height = img.naturalHeight || img.height || 0;
        if (width < 400 || height < 400) continue;
        const existing = imageMap.get(url);
        if (!existing || width * height > existing.width * existing.height) {
          imageMap.set(url, {url, width, height});
        }
      }
      return {
        title,
        description,
        author,
        sourceUrls: Array.from(new Set(sourceUrls)),
        images: Array.from(imageMap.values()),
      };
    })()
    """
    payload = session.evaluate(script, await_promise=False) or {}
    source_urls = [normalize_media_url(url) for url in payload.get("sourceUrls") or [] if normalize_media_url(url)]
    image_entries = payload.get("images") or []

    if not source_urls and not image_entries:
        return None

    variants: list[dict] = []
    for url in source_urls:
        parsed = parse_qs(urlparse(url).query)
        bit_rate = int((parsed.get("bt") or parsed.get("br") or ["0"])[0] or 0)
        variants.append(
            {
                "source": "page_source",
                "source_priority": 5,
                "bit_rate": bit_rate,
                "width": 0,
                "height": 0,
                "data_size": 0,
                "urls": [url],
            }
        )

    images = [
        {
            "uri": None,
            "width": image.get("width"),
            "height": image.get("height"),
            "urls": [normalize_media_url(image.get("url"))],
        }
        for image in image_entries
        if normalize_media_url(image.get("url"))
    ]

    return {
        "aweme_id": aweme_id,
        "desc": (payload.get("title") or payload.get("description") or "").strip(),
        "create_time": None,
        "aweme_type": 68 if images and not variants else 0,
        "author": {
            "nickname": (payload.get("author") or "").strip(),
            "uid": "",
            "unique_id": "",
            "sec_uid": "",
        },
        "video": {
            "width": 0,
            "height": 0,
            "variants": variants,
            "play_urls": source_urls,
            "download_urls": [],
            "bit_rates": [],
        },
        "images": images,
        "original_images": images,
    }


def launch_browser(
    url: str, browser: str, browser_profile: str, target_fragment: str
) -> tuple[HiddenBrowserHandle, Path, CDPSession]:
    user_data_dir = copy_browser_profile(browser_profile)
    port = allocate_port()
    browser_bin = chrome_binary(browser)
    process = launch_hidden_browser(
        browser_bin,
        user_data_dir=user_data_dir,
        port=port,
        url=url,
    )

    wait_for_debug_port(port)
    target = wait_for_target_fragment(port, target_fragment)
    session = CDPSession(target["webSocketDebuggerUrl"])
    session.call("Runtime.enable")
    return process, user_data_dir, session


def launch_browser_for_profile(sec_uid: str, browser: str, browser_profile: str) -> tuple[subprocess.Popen, Path, CDPSession]:
    return launch_browser(
        f"https://www.douyin.com/user/{sec_uid}",
        browser,
        browser_profile,
        f"douyin.com/user/{sec_uid}",
    )


def launch_browser_for_video(aweme_id: str, browser: str, browser_profile: str) -> tuple[subprocess.Popen, Path, CDPSession]:
    return launch_browser(
        f"https://www.douyin.com/video/{aweme_id}",
        browser,
        browser_profile,
        f"douyin.com/video/{aweme_id}",
    )


def normalize_media_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def normalize_video_variant(
    addr: dict | None,
    *,
    source: str,
    bit_rate: int = 0,
    source_priority: int = 0,
) -> dict | None:
    if not isinstance(addr, dict):
        return None
    urls = [
        normalize_media_url(url)
        for url in (addr.get("url_list") or [])
        if normalize_media_url(url)
    ]
    if not urls:
        return None
    return {
        "source": source,
        "source_priority": source_priority,
        "bit_rate": bit_rate,
        "width": addr.get("width"),
        "height": addr.get("height"),
        "data_size": addr.get("data_size"),
        "urls": urls,
    }


def parse_misc_download_addrs(raw_value) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str) and raw_value:
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def extract_items(page_data: dict) -> list[dict]:
    items: list[dict] = []
    for aweme in page_data.get("aweme_list") or []:
        video = aweme.get("video") or {}
        bit_rates = []
        variants: list[dict] = []
        for bit_rate in video.get("bit_rate") or []:
            variant = normalize_video_variant(
                bit_rate.get("play_addr"),
                source=bit_rate.get("gear_name") or "bit_rate",
                bit_rate=int(bit_rate.get("bit_rate") or 0),
                source_priority=4,
            )
            if variant:
                variants.append(variant)
            bit_rates.append(
                {
                    "gear_name": bit_rate.get("gear_name"),
                    "bit_rate": bit_rate.get("bit_rate"),
                    "width": ((bit_rate.get("play_addr") or {}).get("width")),
                    "height": ((bit_rate.get("play_addr") or {}).get("height")),
                    "data_size": ((bit_rate.get("play_addr") or {}).get("data_size")),
                    "urls": [
                        normalize_media_url(url)
                        for url in ((bit_rate.get("play_addr") or {}).get("url_list") or [])
                        if normalize_media_url(url)
                    ],
                }
            )

        for source_name, addr, source_priority in (
            ("play_addr", video.get("play_addr"), 3),
            ("play_addr_h264", video.get("play_addr_h264"), 3),
            ("play_addr_265", video.get("play_addr_265"), 3),
            ("download_addr", video.get("download_addr"), 1),
        ):
            variant = normalize_video_variant(
                addr,
                source=source_name,
                bit_rate=0,
                source_priority=source_priority,
            )
            if variant:
                variants.append(variant)

        for source_name, addr in parse_misc_download_addrs(video.get("misc_download_addrs")).items():
            variant = normalize_video_variant(
                addr,
                source=f"misc:{source_name}",
                bit_rate=0,
                source_priority=2,
            )
            if variant:
                variants.append(variant)

        def normalize_images(raw_images: list[dict] | None) -> list[dict]:
            images: list[dict] = []
            for image in raw_images or []:
                urls: list[str] = []
                for key in ("watermark_free_download_url_list", "url_list", "download_url_list"):
                    urls.extend(
                        normalize_media_url(url)
                        for url in (image.get(key) or [])
                        if normalize_media_url(url)
                    )
                deduped_urls: list[str] = []
                seen: set[str] = set()
                for url in urls:
                    if url in seen:
                        continue
                    seen.add(url)
                    deduped_urls.append(url)
                urls = deduped_urls
                if not urls:
                    continue
                images.append(
                    {
                        "uri": image.get("uri"),
                        "width": image.get("width"),
                        "height": image.get("height"),
                        "urls": urls,
                    }
                )
            return images

        items.append(
            {
                "aweme_id": aweme.get("aweme_id"),
                "desc": aweme.get("desc") or "",
                "create_time": aweme.get("create_time"),
                "aweme_type": aweme.get("aweme_type"),
                "author": {
                    "nickname": ((aweme.get("author") or {}).get("nickname")) or "",
                    "uid": ((aweme.get("author") or {}).get("uid")) or "",
                    "unique_id": ((aweme.get("author") or {}).get("unique_id")) or "",
                    "sec_uid": ((aweme.get("author") or {}).get("sec_uid")) or "",
                },
                "video": {
                    "width": video.get("width"),
                    "height": video.get("height"),
                    "variants": variants,
                    "play_urls": [
                        normalize_media_url(url)
                        for url in ((video.get("play_addr") or {}).get("url_list") or [])
                        if normalize_media_url(url)
                    ],
                    "download_urls": [
                        normalize_media_url(url)
                        for url in ((video.get("download_addr") or {}).get("url_list") or [])
                        if normalize_media_url(url)
                    ],
                    "bit_rates": bit_rates,
                },
                "images": normalize_images(aweme.get("images")),
                "original_images": normalize_images(aweme.get("original_images")),
            }
        )
    return items


def fetch_all_posts(sec_uid: str, browser: str, browser_profile: str, page_size: int, max_pages: int | None) -> list[dict]:
    process, user_data_dir, session = launch_browser_for_profile(sec_uid, browser, browser_profile)
    try:
        for _ in range(24):
            title = page_title(session)
            if title and "验证码" not in title:
                break
            time.sleep(1)
        if "验证码" in page_title(session):
            raise RuntimeError("Douyin opened a captcha interstitial. Open the profile in Chrome once, then retry.")

        meta = profile_page_meta(session)
        declared_post_count = meta.get("declared_post_count")
        login_gate_visible = bool(meta.get("login_gate_visible"))
        request_template = discover_post_request_template(session)

        all_items: list[dict] = []
        seen_ids: set[str] = set()
        cursor = "0"
        page_number = 0

        while True:
            page_number += 1
            if request_template:
                page_json = session.evaluate(build_signed_page_fetch_expression(request_template, cursor, page_size))
            else:
                page_json = session.evaluate(build_page_fetch_expression(sec_uid, cursor, page_size))
            page_data = json.loads(page_json)
            batch = extract_items(page_data)
            for item in batch:
                aweme_id = item.get("aweme_id")
                if aweme_id and aweme_id not in seen_ids:
                    seen_ids.add(aweme_id)
                    all_items.append(item)

            print(f"Fetched page {page_number}: {len(batch)} items, total {len(all_items)}", flush=True)

            if not page_data.get("has_more"):
                break
            if max_pages and page_number >= max_pages:
                break
            next_cursor = page_data.get("max_cursor")
            if next_cursor in (None, "", cursor):
                break
            cursor = str(next_cursor)
            time.sleep(1.2)

        if declared_post_count and len(all_items) < declared_post_count and login_gate_visible:
            raise RuntimeError(
                "Douyin web is limiting this browser session to a partial result. "
                f"The profile page shows {declared_post_count} posts, but this browser session only exposed {len(all_items)} "
                "before the '登录后查看更多作品' gate. Log into Douyin in the selected browser profile and rerun."
            )

        return all_items
    finally:
        session.close()
        terminate_hidden_browser(process)
        shutil.rmtree(user_data_dir, ignore_errors=True)


def fetch_single_post(aweme_id: str, browser: str, browser_profile: str) -> dict:
    process, user_data_dir, session = launch_browser_for_video(aweme_id, browser, browser_profile)
    try:
        wait_for_video_page_ready(session, aweme_id)
        if "验证码" in page_title(session):
            raise RuntimeError("Douyin opened a captcha interstitial. Open the video in Chrome once, then retry.")

        request_template = discover_detail_request_template(session, aweme_id)
        primary_error: Exception | None = None
        for expression in (
            build_detail_fetch_expression(aweme_id),
            build_resource_fetch_expression(request_template) if request_template else None,
        ):
            if not expression:
                continue
            try:
                page_json = session.evaluate(expression)
                page_data = json.loads(page_json)
                if isinstance(page_data.get("aweme_detail"), dict):
                    page_data = {"aweme_list": [page_data["aweme_detail"]]}
                items = extract_items(page_data)
                if items:
                    return items[0]
            except Exception as exc:
                primary_error = exc

        fallback_item = extract_single_post_from_dom(session, aweme_id)
        if fallback_item:
            return fallback_item

        if primary_error:
            raise primary_error
        raise RuntimeError(f"No media payload returned for aweme_id {aweme_id}.")
    finally:
        session.close()
        terminate_hidden_browser(process)
        shutil.rmtree(user_data_dir, ignore_errors=True)


def media_opener(browser: str, browser_profile: str) -> urllib.request.OpenerDirector:
    cookie_jar = extract_cookies_from_browser(browser, browser_profile)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Referer", "https://www.douyin.com/"),
        ("Accept", "*/*"),
    ]
    return opener


def infer_extension(url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".mp4", ".mov", ".webm"}:
        return suffix
    if content_type:
        normalized = content_type.split(";")[0].strip().lower()
        if normalized in EXT_BY_CONTENT_TYPE:
            return EXT_BY_CONTENT_TYPE[normalized]
        guessed = mimetypes.guess_extension(normalized)
        if guessed:
            return guessed
    return ""


def download_url(opener: urllib.request.OpenerDirector, url: str, destination_stem: Path) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"})
    with opener.open(request, timeout=60) as response:
        content_type = response.headers.get("Content-Type")
        extension = infer_extension(url, content_type)
        destination = destination_stem.with_suffix(extension)
        tmp_destination = destination.with_suffix(destination.suffix + ".part")
        with tmp_destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    tmp_destination.replace(destination)
    return destination


def best_video_candidates(item: dict) -> list[str]:
    ranked: list[tuple[tuple[int, int, int, int], str]] = []

    for variant in item["video"].get("variants") or []:
        score = (
            int(variant.get("width") or 0) * int(variant.get("height") or 0),
            int(variant.get("bit_rate") or 0),
            int(variant.get("data_size") or 0),
            int(variant.get("source_priority") or 0),
        )
        for url in variant.get("urls") or []:
            ranked.append((score, url))

    if not ranked:
        for bit_rate in item["video"]["bit_rates"]:
            score = (
                int(bit_rate.get("width") or 0) * int(bit_rate.get("height") or 0),
                int(bit_rate.get("bit_rate") or 0),
                int(bit_rate.get("data_size") or 0),
                4,
            )
            for url in bit_rate.get("urls") or []:
                ranked.append((score, url))

        for url in item["video"]["play_urls"]:
            score = (
                int(item["video"].get("width") or 0) * int(item["video"].get("height") or 0),
                0,
                0,
                3,
            )
            ranked.append((score, url))

        for url in item["video"]["download_urls"]:
            ranked.append(((0, 0, 0, 1), url))

    seen: set[str] = set()
    ordered = []
    for _, url in sorted(ranked, key=lambda item: item[0], reverse=True):
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def best_image_groups(item: dict) -> list[dict]:
    if item["original_images"]:
        return item["original_images"]
    return item["images"]


def is_photo_post(item: dict) -> bool:
    return item.get("aweme_type") == 68 and bool(best_image_groups(item))


def download_profile(
    items: list[dict],
    output_root: Path | None,
    browser: str,
    browser_profile: str,
    sec_uid: str,
) -> Path:
    if not items:
        raise RuntimeError("No posts were returned from the Douyin profile.")

    nickname = items[0]["author"]["nickname"] or sec_uid
    base_dir = allocate_output_dir(
        "douyin",
        str(output_root) if output_root else None,
        nickname,
        default_name="douyin_user",
    )
    image_dir = base_dir / "图片"
    video_dir = base_dir / "视频"
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    opener = media_opener(browser, browser_profile)
    stats = {"images": 0, "videos": 0, "failed": 0}

    for index, item in enumerate(items, start=1):
        aweme_id = item.get("aweme_id") or f"item_{index:04d}"
        if is_photo_post(item):
            groups = best_image_groups(item)
            print(f"[{index}/{len(items)}] Images {aweme_id} x{len(groups)}", flush=True)
            for image_index, image in enumerate(groups, start=1):
                target_stem = image_dir / f"{aweme_id}_{image_index:02d}"
                if any(target_stem.with_suffix(ext).exists() for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", "")):
                    continue
                success = False
                for url in image.get("urls") or []:
                    try:
                        download_url(opener, url, target_stem)
                        stats["images"] += 1
                        success = True
                        break
                    except Exception:
                        continue
                if not success:
                    stats["failed"] += 1
        else:
            candidates = best_video_candidates(item)
            print(f"[{index}/{len(items)}] Video {aweme_id}", flush=True)
            target_stem = video_dir / aweme_id
            if any(target_stem.with_suffix(ext).exists() for ext in (".mp4", ".mov", ".webm", "")):
                continue
            success = False
            for url in candidates:
                try:
                    download_url(opener, url, target_stem)
                    stats["videos"] += 1
                    success = True
                    break
                except Exception:
                    continue
            if not success:
                stats["failed"] += 1

    total_images = sum(
        1 for path in image_dir.iterdir() if path.is_file()
    )
    total_videos = sum(
        1 for path in video_dir.iterdir() if path.is_file()
    )

    manifest = {
        "target_kind": "profile",
        "nickname": nickname,
        "sec_uid": sec_uid,
        "posts": len(items),
        "images_downloaded": total_images,
        "videos_downloaded": total_videos,
        "images_downloaded_this_run": stats["images"],
        "videos_downloaded_this_run": stats["videos"],
        "failed_items": stats["failed"],
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (base_dir / "download_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Done. Folder: {base_dir}\nImages {stats['images']}, videos {stats['videos']}, failed {stats['failed']}",
        flush=True,
    )
    return base_dir


def download_single_post(
    item: dict,
    output_root: Path | None,
    browser: str,
    browser_profile: str,
    aweme_id: str,
    source_url: str,
) -> Path:
    nickname = item["author"]["nickname"] or aweme_id
    base_dir = allocate_output_dir(
        "douyin",
        str(output_root) if output_root else None,
        nickname,
        default_name="douyin_user",
    )
    image_dir = base_dir / "图片"
    video_dir = base_dir / "视频"
    image_dir.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)

    opener = media_opener(browser, browser_profile)
    stats = {"images": 0, "videos": 0, "failed": 0}

    if is_photo_post(item):
        groups = best_image_groups(item)
        print(f"Images {aweme_id} x{len(groups)}", flush=True)
        for image_index, image in enumerate(groups, start=1):
            target_stem = image_dir / f"{aweme_id}_{image_index:02d}"
            if any(target_stem.with_suffix(ext).exists() for ext in (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", "")):
                continue
            success = False
            for url in image.get("urls") or []:
                try:
                    download_url(opener, url, target_stem)
                    stats["images"] += 1
                    success = True
                    break
                except Exception:
                    continue
            if not success:
                stats["failed"] += 1
    else:
        print(f"Video {aweme_id}", flush=True)
        target_stem = video_dir / aweme_id
        if not any(target_stem.with_suffix(ext).exists() for ext in (".mp4", ".mov", ".webm", "")):
            success = False
            for url in best_video_candidates(item):
                try:
                    download_url(opener, url, target_stem)
                    stats["videos"] += 1
                    success = True
                    break
                except Exception:
                    continue
            if not success:
                stats["failed"] += 1

    total_images = sum(1 for path in image_dir.iterdir() if path.is_file())
    total_videos = sum(1 for path in video_dir.iterdir() if path.is_file())
    manifest = {
        "target_kind": "single",
        "aweme_id": aweme_id,
        "post_url": source_url,
        "nickname": nickname,
        "images_downloaded": total_images,
        "videos_downloaded": total_videos,
        "images_downloaded_this_run": stats["images"],
        "videos_downloaded_this_run": stats["videos"],
        "failed_items": stats["failed"],
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (base_dir / "download_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Done. Folder: {base_dir}\nImages {stats['images']}, videos {stats['videos']}, failed {stats['failed']}",
        flush=True,
    )
    return base_dir


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else None

    try:
        raw_url = extract_candidate_url(args.share_text_or_url)
        resolved_url = canonicalize_douyin_url(resolve_share_url(raw_url, args.request_timeout))
        target_kind = args.target_kind
        if target_kind == "auto":
            target_kind = detect_target_kind(resolved_url)

        if target_kind == "single":
            aweme_id = extract_aweme_id(resolved_url)
            print(f"Resolved aweme_id: {aweme_id}", flush=True)
            item = fetch_single_post(
                aweme_id=aweme_id,
                browser=args.browser,
                browser_profile=args.browser_profile,
            )
            download_single_post(
                item=item,
                output_root=output_root,
                browser=args.browser,
                browser_profile=args.browser_profile,
                aweme_id=aweme_id,
                source_url=resolved_url,
            )
        else:
            sec_uid = extract_sec_uid(resolved_url)
            print(f"Resolved sec_uid: {sec_uid}", flush=True)
            items = fetch_all_posts(
                sec_uid=sec_uid,
                browser=args.browser,
                browser_profile=args.browser_profile,
                page_size=args.page_size,
                max_pages=args.max_pages,
            )
            download_profile(
                items=items,
                output_root=output_root,
                browser=args.browser,
                browser_profile=args.browser_profile,
                sec_uid=sec_uid,
            )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

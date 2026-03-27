from __future__ import annotations

import re


TRIM_CHARS = " \t\r\n\"'`<>[](){}。，、；：！？,.!?:;）】》"


def extract_url_from_text(
    text: str,
    hostnames: tuple[str, ...],
    *,
    field_name: str = "Target",
) -> str:
    raw = (text or "").strip()
    if not raw:
        raise ValueError(f"{field_name} cannot be empty.")

    escaped_hosts = "|".join(re.escape(hostname) for hostname in hostnames)
    match = re.search(
        rf"(?:https?://)?(?:[\w-]+\.)?(?:{escaped_hosts})/[^\s]+",
        raw,
        re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Could not find a supported URL in the provided {field_name.lower()}.")

    candidate = match.group(0).strip(TRIM_CHARS)
    if not candidate.startswith(("http://", "https://")):
        candidate = "https://" + candidate
    return candidate

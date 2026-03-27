from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TARGET_KIND_PROFILE = "profile"
TARGET_KIND_SINGLE = "single"
TARGET_KIND_CHOICES = {
    TARGET_KIND_PROFILE,
    TARGET_KIND_SINGLE,
}

TERMINAL_STATUSES = {
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_CANCELLED,
}


@dataclass(slots=True)
class TaskCreate:
    platform: str
    target_kind: str
    input_url: str
    browser: str | None = None
    browser_profile: str | None = None
    output_root_override: str | None = None


def now_string() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HiddenBrowserHandle:
    user_data_dir: Path
    app_binary: Path
    pids: list[int]


def app_bundle_from_binary(app_binary: Path) -> Path:
    return app_binary.parents[2]


def browser_process_pids(user_data_dir: Path) -> list[int]:
    pattern = f"--user-data-dir={user_data_dir}"
    output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    pids: list[int] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or pattern not in line:
            continue
        pid_text, _, _ = line.partition(" ")
        if pid_text.isdigit():
            pids.append(int(pid_text))
    return pids


def wait_for_browser_process(user_data_dir: Path, timeout_seconds: int = 30) -> list[int]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pids = browser_process_pids(user_data_dir)
        if pids:
            return pids
        time.sleep(0.25)
    raise RuntimeError(f"Could not find Chrome process for user data dir {user_data_dir}.")


def launch_hidden_browser(
    app_binary: Path,
    *,
    user_data_dir: Path,
    port: int,
    url: str,
) -> HiddenBrowserHandle:
    launcher = subprocess.Popen(
        [
            "open",
            "-n",
            "-g",
            "-j",
            "-a",
            str(app_bundle_from_binary(app_binary)),
            "--args",
            f"--user-data-dir={user_data_dir}",
            "--profile-directory=Default",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--window-position=-32000,-32000",
            "--window-size=1280,900",
            "--start-minimized",
            "--new-window",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    launcher.wait(timeout=15)
    pids = wait_for_browser_process(user_data_dir)
    return HiddenBrowserHandle(user_data_dir=user_data_dir, app_binary=app_binary, pids=pids)


def terminate_hidden_browser(handle: HiddenBrowserHandle, timeout_seconds: int = 5) -> None:
    pids = browser_process_pids(handle.user_data_dir) or handle.pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not browser_process_pids(handle.user_data_dir):
            return
        time.sleep(0.25)

    for pid in browser_process_pids(handle.user_data_dir):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue

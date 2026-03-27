from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.toml"
EXAMPLE_CONFIG_PATH = ROOT_DIR / "config.example.toml"


@dataclass(slots=True)
class AppConfig:
    host: str
    port: int
    download_root: Path
    data_dir: Path
    log_dir: Path


@dataclass(slots=True)
class BrowserConfig:
    default_browser: str
    default_profile: str


@dataclass(slots=True)
class QueueConfig:
    parallel_tasks: int
    task_timeout_seconds: int
    max_task_history: int


@dataclass(slots=True)
class Settings:
    app: AppConfig
    browser: BrowserConfig
    queue: QueueConfig


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _as_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    return path


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or DEFAULT_CONFIG_PATH
    source = path if path.exists() else EXAMPLE_CONFIG_PATH
    data = _read_toml(source)

    app_data = data.get("app", {})
    browser_data = data.get("browser", {})
    queue_data = data.get("queue", {})

    settings = Settings(
        app=AppConfig(
            host=str(app_data.get("host", "127.0.0.1")),
            port=int(app_data.get("port", 8123)),
            download_root=_as_path(str(app_data.get("download_root", "./platform_downloads"))),
            data_dir=_as_path(str(app_data.get("data_dir", "./var/data"))),
            log_dir=_as_path(str(app_data.get("log_dir", "./var/log"))),
        ),
        browser=BrowserConfig(
            default_browser=str(browser_data.get("default_browser", "chrome")),
            default_profile=str(browser_data.get("default_profile", "Default")),
        ),
        queue=QueueConfig(
            parallel_tasks=max(1, int(queue_data.get("parallel_tasks", 1))),
            task_timeout_seconds=max(60, int(queue_data.get("task_timeout_seconds", 7200))),
            max_task_history=max(20, int(queue_data.get("max_task_history", 200))),
        ),
    )
    ensure_runtime_dirs(settings)
    return settings


def ensure_runtime_dirs(settings: Settings) -> None:
    settings.app.download_root.mkdir(parents=True, exist_ok=True)
    settings.app.data_dir.mkdir(parents=True, exist_ok=True)
    settings.app.log_dir.mkdir(parents=True, exist_ok=True)


def save_settings(settings: Settings, config_path: Path | None = None) -> Path:
    path = config_path or DEFAULT_CONFIG_PATH
    ensure_runtime_dirs(settings)
    content = "\n".join(
        [
            "[app]",
            f'host = "{settings.app.host}"',
            f"port = {settings.app.port}",
            f'download_root = "{settings.app.download_root}"',
            f'data_dir = "{settings.app.data_dir}"',
            f'log_dir = "{settings.app.log_dir}"',
            "",
            "[browser]",
            f'default_browser = "{settings.browser.default_browser}"',
            f'default_profile = "{settings.browser.default_profile}"',
            "",
            "[queue]",
            f"parallel_tasks = {settings.queue.parallel_tasks}",
            f"task_timeout_seconds = {settings.queue.task_timeout_seconds}",
            f"max_task_history = {settings.queue.max_task_history}",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    return path

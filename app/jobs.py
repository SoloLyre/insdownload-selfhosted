from __future__ import annotations

import queue
import subprocess
import threading
from pathlib import Path
from typing import Any

from app import db
from app.config import Settings
from app.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    TERMINAL_STATUSES,
    now_string,
)
from app.platforms import get_adapter
from app.platforms.base import discover_output_dir


class JobRunner:
    def __init__(self, settings: Settings, db_path: Path):
        self.settings = settings
        self.db_path = db_path
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="insdownload-job-runner")
        self._lock = threading.Lock()
        self._current_task_id: str | None = None
        self._current_process: subprocess.Popen[str] | None = None
        self._cancelled_pending: set[str] = set()
        self._cancel_requested_running: set[str] = set()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            process = self._current_process
        if process and process.poll() is None:
            process.terminate()
        self._queue.put("__stop__")
        self._thread.join(timeout=5)

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def enqueue(self, task_id: str) -> None:
        self._queue.put(task_id)

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        task = db.get_task(self.db_path, task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return task
        with self._lock:
            if self._current_task_id == task_id and self._current_process and self._current_process.poll() is None:
                self._cancel_requested_running.add(task_id)
                self._current_process.terminate()
                return db.update_task(self.db_path, task_id, error_message="Cancellation requested.")
        self._cancelled_pending.add(task_id)
        return db.update_task(
            self.db_path,
            task_id,
            status=STATUS_CANCELLED,
            finished_at=now_string(),
            error_message="Cancelled before execution.",
        )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            task_id = self._queue.get()
            if task_id == "__stop__":
                break
            if task_id in self._cancelled_pending:
                self._cancelled_pending.discard(task_id)
                continue
            try:
                self._run_task(task_id)
            except Exception as exc:  # noqa: BLE001
                db.update_task(
                    self.db_path,
                    task_id,
                    status=STATUS_FAILED,
                    finished_at=now_string(),
                    error_message=str(exc),
                )

    def _run_task(self, task_id: str) -> None:
        task = db.get_task(self.db_path, task_id)
        if task is None or task["status"] in TERMINAL_STATUSES:
            return

        adapter = get_adapter(task["platform"])
        output_root_base = Path(task["output_root_override"]).expanduser().resolve() if task.get("output_root_override") else self.settings.app.download_root
        output_root = output_root_base / adapter.output_platform_key
        before_snapshot = set()
        if output_root.exists():
            for path in output_root.iterdir():
                if path.is_dir():
                    before_snapshot.add(str(path.resolve()))
        output_root.mkdir(parents=True, exist_ok=True)

        log_path = self.settings.app.log_dir / f"{task_id}.log"
        command = adapter.build_command(task, self.settings, output_root)
        db.update_task(
            self.db_path,
            task_id,
            status=STATUS_RUNNING,
            started_at=now_string(),
            output_root=str(output_root),
            log_path=str(log_path),
            error_message=None,
        )

        with log_path.open("w", encoding="utf-8") as log_handle:
            log_handle.write("$ " + " ".join(command) + "\n\n")
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parent.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with self._lock:
                self._current_task_id = task_id
                self._current_process = process
            db.update_task(self.db_path, task_id, process_id=process.pid)

            assert process.stdout is not None
            for line in process.stdout:
                log_handle.write(line)
                log_handle.flush()
            exit_code = process.wait()

        with self._lock:
            self._current_task_id = None
            self._current_process = None

        if task_id in self._cancel_requested_running:
            self._cancel_requested_running.discard(task_id)
            db.update_task(
                self.db_path,
                task_id,
                status=STATUS_CANCELLED,
                finished_at=now_string(),
                exit_code=exit_code,
                error_message="Cancelled during execution.",
            )
            return

        output_dir = discover_output_dir(output_root, before_snapshot)
        result: dict[str, Any] | None = None
        if output_dir is not None:
            result = adapter.collect_result(task, output_root, output_dir)

        if exit_code == 0:
            db.update_task(
                self.db_path,
                task_id,
                status=STATUS_SUCCESS,
                finished_at=now_string(),
                exit_code=exit_code,
                output_dir=result["output_dir"] if result else None,
                manifest_path=result["manifest_path"] if result else None,
                result=result,
            )
        else:
            db.update_task(
                self.db_path,
                task_id,
                status=STATUS_FAILED,
                finished_at=now_string(),
                exit_code=exit_code,
                output_dir=result["output_dir"] if result else None,
                manifest_path=result["manifest_path"] if result else None,
                result=result,
                error_message=f"Downloader exited with status {exit_code}.",
            )

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.models import STATUS_PENDING, now_string


SCHEMA = """
create table if not exists tasks (
    id text primary key,
    platform text not null,
    target_kind text not null default 'profile',
    input_url text not null,
    status text not null,
    created_at text not null,
    started_at text,
    finished_at text,
    browser text,
    browser_profile text,
    output_root_override text,
    output_root text,
    output_dir text,
    manifest_path text,
    log_path text,
    exit_code integer,
    error_message text,
    process_id integer,
    result_json text
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.executescript(SCHEMA)
        columns = {
            row["name"]
            for row in connection.execute("pragma table_info(tasks)").fetchall()
        }
        if "target_kind" not in columns:
            connection.execute(
                "alter table tasks add column target_kind text not null default 'profile'"
            )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if data.get("result_json"):
        data["result"] = json.loads(data["result_json"])
    else:
        data["result"] = None
    data.pop("result_json", None)
    return data


def create_task(
    db_path: Path,
    *,
    platform: str,
    target_kind: str,
    input_url: str,
    browser: str | None,
    browser_profile: str | None,
    output_root_override: str | None,
) -> dict[str, Any]:
    task_id = uuid.uuid4().hex[:12]
    created_at = now_string()
    with connect(db_path) as connection:
        connection.execute(
            """
            insert into tasks (
                id, platform, target_kind, input_url, status, created_at,
                browser, browser_profile, output_root_override
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                platform,
                target_kind,
                input_url,
                STATUS_PENDING,
                created_at,
                browser,
                browser_profile,
                output_root_override,
            ),
        )
        row = connection.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    task = _row_to_dict(row)
    if task is None:
        raise RuntimeError("Failed to create task record.")
    return task


def update_task(db_path: Path, task_id: str, **fields: Any) -> dict[str, Any]:
    if not fields:
        task = get_task(db_path, task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    serialized_fields = dict(fields)
    if "result" in serialized_fields:
        result = serialized_fields.pop("result")
        serialized_fields["result_json"] = json.dumps(result, ensure_ascii=False) if result is not None else None

    columns = ", ".join(f"{key} = ?" for key in serialized_fields)
    values = list(serialized_fields.values()) + [task_id]

    with connect(db_path) as connection:
        connection.execute(f"update tasks set {columns} where id = ?", values)
        row = connection.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    task = _row_to_dict(row)
    if task is None:
        raise KeyError(task_id)
    return task


def get_task(db_path: Path, task_id: str) -> dict[str, Any] | None:
    with connect(db_path) as connection:
        row = connection.execute("select * from tasks where id = ?", (task_id,)).fetchone()
    return _row_to_dict(row)


def list_tasks(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    with connect(db_path) as connection:
        rows = connection.execute(
            "select * from tasks order by created_at desc limit ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows if row is not None]

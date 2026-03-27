from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.config import DEFAULT_CONFIG_PATH, Settings, load_settings, save_settings
from app.jobs import JobRunner
from app.models import TARGET_KIND_CHOICES, TARGET_KIND_PROFILE, TaskCreate
from app.platforms import get_adapter, platform_choices


TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


@dataclass(slots=True)
class AppState:
    settings: Settings
    db_path: Path
    runner: JobRunner


def _db_path_for(settings: Settings) -> Path:
    return settings.app.data_dir / "insdownload.sqlite3"


def _serialize_task(task: dict[str, Any]) -> dict[str, Any]:
    task = dict(task)
    adapter = get_adapter(task["platform"])
    task["platform_label"] = adapter.label
    task["target_kind_label"] = "Single Post" if task.get("target_kind") == "single" else "Profile"
    return task


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or load_settings()
    db_path = _db_path_for(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db(db_path)
        runner = JobRunner(resolved_settings, db_path)
        runner.start()
        app.state.insdownload = AppState(
            settings=resolved_settings,
            db_path=db_path,
            runner=runner,
        )
        yield
        runner.stop()

    app = FastAPI(title="insdownload self-hosted", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

    def state_from(request: Request) -> AppState:
        return request.app.state.insdownload

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        state = state_from(request)
        tasks = [_serialize_task(task) for task in db.list_tasks(state.db_path, limit=state.settings.queue.max_task_history)]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "request": request,
                "settings": state.settings,
                "platforms": platform_choices(),
                "tasks": tasks,
            },
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str) -> HTMLResponse:
        state = state_from(request)
        task = db.get_task(state.db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return TEMPLATES.TemplateResponse(
            request=request,
            name="task.html",
            context={
                "request": request,
                "task": _serialize_task(task),
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        state = state_from(request)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "request": request,
                "settings": state.settings,
                "config_path": DEFAULT_CONFIG_PATH,
            },
        )

    @app.get("/api/tasks")
    async def api_list_tasks(request: Request) -> JSONResponse:
        state = state_from(request)
        tasks = [_serialize_task(task) for task in db.list_tasks(state.db_path, limit=state.settings.queue.max_task_history)]
        return JSONResponse({"tasks": tasks})

    @app.post("/api/tasks")
    async def api_create_task(request: Request) -> JSONResponse:
        state = state_from(request)
        payload = await request.json()
        platform = str(payload.get("platform", "")).strip()
        target_kind = str(payload.get("target_kind", TARGET_KIND_PROFILE)).strip() or TARGET_KIND_PROFILE
        input_url = str(payload.get("input_url", "")).strip()
        if not platform or not input_url:
            raise HTTPException(status_code=400, detail="platform and input_url are required.")
        if target_kind not in TARGET_KIND_CHOICES:
            raise HTTPException(status_code=400, detail=f"Unsupported target kind: {target_kind}")
        try:
            adapter = get_adapter(platform)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        create = TaskCreate(
            platform=adapter.platform_id,
            target_kind=target_kind,
            input_url=input_url,
            browser=str(payload.get("browser", "")).strip() or None,
            browser_profile=str(payload.get("browser_profile", "")).strip() or None,
            output_root_override=str(payload.get("output_root_override", "")).strip() or None,
        )
        task = db.create_task(
            state.db_path,
            platform=create.platform,
            target_kind=create.target_kind,
            input_url=create.input_url,
            browser=create.browser,
            browser_profile=create.browser_profile,
            output_root_override=create.output_root_override,
        )
        state.runner.enqueue(task["id"])
        return JSONResponse({"task": _serialize_task(task)}, status_code=201)

    @app.get("/api/tasks/{task_id}")
    async def api_get_task(request: Request, task_id: str) -> JSONResponse:
        state = state_from(request)
        task = db.get_task(state.db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return JSONResponse({"task": _serialize_task(task)})

    @app.get("/api/tasks/{task_id}/log")
    async def api_task_log(request: Request, task_id: str) -> PlainTextResponse:
        state = state_from(request)
        task = db.get_task(state.db_path, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        log_path = Path(task["log_path"]) if task.get("log_path") else None
        content = log_path.read_text(encoding="utf-8") if log_path and log_path.exists() else ""
        return PlainTextResponse(content)

    @app.post("/api/tasks/{task_id}/cancel")
    async def api_cancel_task(request: Request, task_id: str) -> JSONResponse:
        state = state_from(request)
        task = state.runner.cancel(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return JSONResponse({"task": _serialize_task(task)})

    @app.get("/api/settings")
    async def api_get_settings(request: Request) -> JSONResponse:
        state = state_from(request)
        return JSONResponse(
            {
                "settings": {
                    "app": {
                        "host": state.settings.app.host,
                        "port": state.settings.app.port,
                        "download_root": str(state.settings.app.download_root),
                        "data_dir": str(state.settings.app.data_dir),
                        "log_dir": str(state.settings.app.log_dir),
                    },
                    "browser": {
                        "default_browser": state.settings.browser.default_browser,
                        "default_profile": state.settings.browser.default_profile,
                    },
                    "queue": {
                        "parallel_tasks": state.settings.queue.parallel_tasks,
                        "task_timeout_seconds": state.settings.queue.task_timeout_seconds,
                        "max_task_history": state.settings.queue.max_task_history,
                    },
                }
            }
        )

    @app.post("/api/settings")
    async def api_save_settings(request: Request) -> JSONResponse:
        state = state_from(request)
        payload = await request.json()
        new_settings = Settings(
            app=type(state.settings.app)(
                host=str(payload["app"]["host"]).strip() or state.settings.app.host,
                port=int(payload["app"]["port"]),
                download_root=Path(payload["app"]["download_root"]).expanduser().resolve(),
                data_dir=Path(payload["app"]["data_dir"]).expanduser().resolve(),
                log_dir=Path(payload["app"]["log_dir"]).expanduser().resolve(),
            ),
            browser=type(state.settings.browser)(
                default_browser=str(payload["browser"]["default_browser"]).strip() or state.settings.browser.default_browser,
                default_profile=str(payload["browser"]["default_profile"]).strip() or state.settings.browser.default_profile,
            ),
            queue=type(state.settings.queue)(
                parallel_tasks=1,
                task_timeout_seconds=max(60, int(payload["queue"]["task_timeout_seconds"])),
                max_task_history=max(20, int(payload["queue"]["max_task_history"])),
            ),
        )
        save_settings(new_settings)
        state.settings = new_settings
        state.runner.update_settings(new_settings)
        return JSONResponse({"saved": True})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/open")
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/")

    return app


def main() -> None:
    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.app.host, port=settings.app.port)

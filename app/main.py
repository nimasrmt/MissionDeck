"""FastAPI entrypoint for MissionDeck."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import (
    APP_VERSION,
    BODY_LIMIT,
    CAPABILITIES_CACHE_SECONDS,
    CSRF_HEADER,
    SESSION_COOKIE,
    WORKSPACE,
    WORKER_CAP_MAX,
)
from app.db import (
    add_audit,
    get_conn,
    get_setting,
    init_db,
    set_setting,
    utc_now,
)
from app.execution import AppError, MissionService
from app.orca_adapter import OrcaAdapter, OrcaError
from app.native_payloads import project_status
from app.session_monitor import project_sessions
from app.security import (
    SecurityError,
    check_csrf,
    check_host,
    check_origin,
    new_session_token,
    redact,
    resolve_under_workspace,
    sanitize_markdown,
)

# Injectable for tests
_adapter_factory: Any = None
_db_path: Path | None = None
_capabilities_cache: dict[str, Any] = {"at": 0.0, "data": None}
_session_tokens: set[str] = set()


def set_adapter_factory(factory: Any) -> None:
    global _adapter_factory
    _adapter_factory = factory


def set_db_path(path: Path | None) -> None:
    global _db_path
    _db_path = path


def get_adapter() -> OrcaAdapter:
    if _adapter_factory is not None:
        return _adapter_factory()
    return OrcaAdapter()


def get_service() -> MissionService:
    conn = get_conn(_db_path)
    return MissionService(conn, get_adapter())


def error_response(code: str, message: str, details: dict | None = None, status: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db(_db_path)
    # Startup must not create terminals, re-bind runs, or dispatch workers.
    # Existing mission state is a persisted snapshot until explicit reconciliation.
    yield


app = FastAPI(title="MissionDeck", version=APP_VERSION, lifespan=lifespan)


@app.middleware("http")
async def security_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    try:
        check_host(request.headers.get("host"))
        check_origin(request.headers.get("origin"))
    except SecurityError as exc:
        return error_response(exc.code, exc.message, exc.details, status=403)

    # Body size limit
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > BODY_LIMIT:
                return error_response("body_too_large", "Request body exceeds 64 KiB", status=413)
        except ValueError:
            return error_response("bad_request", "Invalid Content-Length", status=400)

    mutating = request.method in ("POST", "PUT", "PATCH", "DELETE")
    # Session endpoint is the bootstrap exception for CSRF
    path = request.url.path
    if mutating and path != "/api/session":
        try:
            check_csrf(
                request.cookies.get(SESSION_COOKIE),
                request.headers.get(CSRF_HEADER),
                mutating=True,
            )
        except SecurityError as exc:
            return error_response(exc.code, exc.message, exc.details, status=403)

    # Read and re-inject body with hard cap
    if mutating and request.method != "GET":
        body = await request.body()
        if len(body) > BODY_LIMIT:
            return error_response("body_too_large", "Request body exceeds 64 KiB", status=413)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(request.scope, receive)

    response = await call_next(request)
    # Never set CORS
    if "access-control-allow-origin" in response.headers:
        del response.headers["access-control-allow-origin"]
    return response


class MissionCreate(BaseModel):
    title: str
    objective: str
    quality_requirements: str
    scope: str
    constraints: str
    coordinator: str


class ApproveBody(BaseModel):
    plan_version: int = 0


class PlanEditBody(BaseModel):
    plan_version: int = Field(ge=0)
    plan: dict[str, Any]


class CancelBody(BaseModel):
    confirm: bool = False


class SettingsBody(BaseModel):
    worker_cap: int | None = None
    auto_refresh_seconds: int | None = None
    redaction_enabled: bool | None = None


@app.get("/api/session")
def api_session(response: Response) -> dict[str, str]:
    token = new_session_token()
    _session_tokens.add(token)
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return {"token": token}


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    adapter = get_adapter()
    orca: dict[str, Any]
    try:
        orca = project_status(adapter.status())
    except OrcaError as exc:
        orca = {"available": False, "version": None, "runtime": {"error": exc.message}}
    return {
        "ok": True,
        "app_version": APP_VERSION,
        "orca": orca,
        "workspace": str(WORKSPACE),
        "checked_at": utc_now(),
    }


@app.get("/api/agent-sessions")
def api_agent_sessions() -> Response:
    """Read-only Orca session snapshot. No prompts, transcripts or dispatches."""
    try:
        payload = project_sessions(get_adapter().agent_list())
    except OrcaError:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "orca_unavailable", "message": "Orca session list unavailable."}},
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        content={**payload, "checked_at": utc_now()},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/capabilities")
def api_capabilities() -> dict[str, Any]:
    now = time.time()
    if _capabilities_cache["data"] and now - float(_capabilities_cache["at"]) < CAPABILITIES_CACHE_SECONDS:
        return _capabilities_cache["data"]
    data = get_service().capabilities()
    _capabilities_cache["at"] = now
    _capabilities_cache["data"] = data
    return data


class FreeModelBody(BaseModel):
    model: str


@app.get("/api/free-models")
def api_free_models() -> dict[str, Any]:
    service = get_service()
    from app.db import get_setting, loads
    return {**loads(get_setting(service.conn, "free_model_catalog"), {}), "selected": get_setting(service.conn, "free_model_pin")}


@app.post("/api/free-models/refresh")
def api_probe_free_models() -> dict[str, Any]:
    data = get_service().refresh_free_models()
    _capabilities_cache.update(data=None, at=0)
    return data


@app.put("/api/free-models/selection")
def api_select_free_model(body: FreeModelBody) -> Any:
    try:
        data = get_service().select_free_model(body.model)
        _capabilities_cache.update(data=None, at=0)
        return data
    except AppError as exc:
        return error_response(exc.code, exc.message, exc.details)


@app.get("/api/missions")
def api_list_missions() -> dict[str, Any]:
    return {"missions": get_service().list_missions()}


@app.post("/api/missions")
def api_create_mission(body: MissionCreate) -> dict[str, Any]:
    try:
        return get_service().create_mission(body.model_dump())
    except AppError as exc:
        return error_response(exc.code, exc.message, exc.details)  # type: ignore[return-value]


@app.get("/api/missions/{mission_id}")
def api_get_mission(mission_id: str) -> dict[str, Any]:
    try:
        return get_service().get_mission(mission_id)
    except AppError as exc:
        return error_response(exc.code, exc.message, exc.details, status=404 if exc.code == "not_found" else 400)  # type: ignore[return-value]


@app.post("/api/missions/{mission_id}/plan")
def api_plan(mission_id: str) -> dict[str, Any]:
    try:
        return get_service().start_plan(mission_id, repair=False)
    except (AppError, OrcaError) as exc:
        code = getattr(exc, "code", "error")
        return error_response(code, str(exc.message), getattr(exc, "details", {}))  # type: ignore[return-value]


@app.post("/api/missions/{mission_id}/plan/repair")
def api_plan_repair(mission_id: str) -> dict[str, Any]:
    try:
        return get_service().start_plan(mission_id, repair=True)
    except (AppError, OrcaError) as exc:
        return error_response(exc.code, exc.message, getattr(exc, "details", {}))  # type: ignore[return-value]


@app.post("/api/missions/{mission_id}/approve")
def api_approve(mission_id: str, body: ApproveBody) -> dict[str, Any]:
    try:
        return get_service().approve(mission_id, body.plan_version)
    except AppError as exc:
        return error_response(exc.code, exc.message, exc.details)  # type: ignore[return-value]


@app.put("/api/missions/{mission_id}/plan")
def api_save_plan(mission_id: str, body: PlanEditBody) -> Any:
    try:
        return get_service().save_plan(mission_id, body.plan, body.plan_version)
    except (AppError, OrcaError) as exc:
        return error_response(exc.code, exc.message, getattr(exc, "details", {}),
                              status=409 if exc.code == "version_conflict" else 400)


@app.post("/api/missions/{mission_id}/team-plan")
def api_team_plan(mission_id: str) -> Any:
    from app.execution import MissionStartLock
    try:
        with MissionStartLock():
            return get_service()._start_plan_locked(mission_id, team=True)
    except (AppError, OrcaError) as exc:
        return error_response(exc.code, exc.message, exc.details)


@app.put("/api/missions/{mission_id}/team-workflow")
def api_save_team_workflow(mission_id: str, body: PlanEditBody) -> Any:
    try:
        return get_service().save_plan(mission_id, {"team_workflow": body.plan}, body.plan_version)
    except AppError as exc:
        return error_response(exc.code, exc.message, exc.details, 409 if exc.code == "version_conflict" else 400)


@app.post("/api/missions/{mission_id}/start")
def api_start(mission_id: str) -> dict[str, Any]:
    try:
        return get_service().start(mission_id)
    except (AppError, OrcaError) as exc:
        return error_response(exc.code, exc.message, getattr(exc, "details", {}))  # type: ignore[return-value]


@app.post("/api/missions/{mission_id}/refresh")
def api_refresh(mission_id: str) -> dict[str, Any]:
    try:
        return get_service().refresh(mission_id)
    except (AppError, OrcaError) as exc:
        return error_response(exc.code, exc.message, getattr(exc, "details", {}))  # type: ignore[return-value]


@app.post("/api/missions/{mission_id}/cancel")
def api_cancel(mission_id: str, body: CancelBody) -> dict[str, Any]:
    try:
        return get_service().cancel(mission_id, body.confirm)
    except AppError as exc:
        return error_response(exc.code, exc.message, exc.details)  # type: ignore[return-value]


@app.post("/api/missions/{mission_id}/tasks/{task_id}/retry")
def api_retry(mission_id: str, task_id: str) -> dict[str, Any]:
    try:
        return get_service().retry_task(mission_id, task_id)
    except (AppError, OrcaError) as exc:
        return error_response(exc.code, exc.message, getattr(exc, "details", {}))  # type: ignore[return-value]


@app.get("/api/missions/{mission_id}/artifacts")
def api_artifacts(mission_id: str) -> dict[str, Any]:
    mission = get_service().get_mission(mission_id)
    return {"artifacts": mission.get("artifacts") or []}


@app.get("/api/missions/{mission_id}/artifacts/{artifact_id}")
def api_artifact_download(mission_id: str, artifact_id: str) -> Response:
    conn = get_conn(_db_path)
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ? AND mission_id = ?",
        (artifact_id, mission_id),
    ).fetchone()
    if row is None:
        return error_response("not_found", "Artifact not found", status=404)
    try:
        path = resolve_under_workspace(row["rel_path"], must_exist=True)
    except SecurityError as exc:
        return error_response(exc.code, exc.message, exc.details, status=403)
    media = "application/octet-stream"
    inline = False
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".json", ".py", ".log", ".csv"):
        media = "text/plain; charset=utf-8"
        inline = True
    elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        media = f"image/{suffix.lstrip('.')}"
        inline = True
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
    )


@app.get("/api/missions/{mission_id}/artifacts/{artifact_id}/preview")
def api_artifact_preview(mission_id: str, artifact_id: str) -> dict[str, Any]:
    conn = get_conn(_db_path)
    row = conn.execute(
        "SELECT * FROM artifacts WHERE id = ? AND mission_id = ?",
        (artifact_id, mission_id),
    ).fetchone()
    if row is None:
        return error_response("not_found", "Artifact not found", status=404)  # type: ignore[return-value]
    try:
        path = resolve_under_workspace(row["rel_path"], must_exist=True)
    except SecurityError as exc:
        return error_response(exc.code, exc.message, exc.details, status=403)  # type: ignore[return-value]
    raw = path.read_bytes()[: 64 * 1024]
    text = raw.decode("utf-8", errors="replace")
    redaction_on = (get_setting(conn, "redaction_enabled", "true") or "true").lower() == "true"
    if redaction_on:
        text = redact(text)
    truncated = path.stat().st_size > 64 * 1024
    return {"text": text, "truncated": truncated, "redacted": redaction_on}


@app.get("/api/missions/{mission_id}/escalation")
def api_escalation(mission_id: str) -> Response:
    md = sanitize_markdown(get_service().escalation_markdown(mission_id))
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{mission_id}-escalation.md"',
        },
    )


@app.get("/api/settings")
def api_get_settings() -> dict[str, Any]:
    conn = get_conn(_db_path)
    return {
        "worker_cap": int(get_setting(conn, "worker_cap", "2") or 2),
        "auto_refresh_seconds": int(get_setting(conn, "auto_refresh_seconds", "0") or 0),
        "redaction_enabled": (get_setting(conn, "redaction_enabled", "true") or "true").lower() == "true",
    }


@app.put("/api/settings")
def api_put_settings(body: SettingsBody) -> dict[str, Any]:
    conn = get_conn(_db_path)
    if body.worker_cap is not None:
        if body.worker_cap < 1 or body.worker_cap > WORKER_CAP_MAX:
            return error_response("validation", f"worker_cap must be 1..{WORKER_CAP_MAX}")  # type: ignore[return-value]
        set_setting(conn, "worker_cap", str(body.worker_cap))
    if body.auto_refresh_seconds is not None:
        if body.auto_refresh_seconds < 0 or body.auto_refresh_seconds > 60:
            return error_response("validation", "auto_refresh_seconds must be 0..60")  # type: ignore[return-value]
        set_setting(conn, "auto_refresh_seconds", str(body.auto_refresh_seconds))
    if body.redaction_enabled is not None:
        set_setting(conn, "redaction_enabled", "true" if body.redaction_enabled else "false")
    add_audit(conn, "settings_update", body.model_dump())
    conn.commit()
    return api_get_settings()


@app.get("/api/audit")
def api_audit(limit: int = 100) -> dict[str, Any]:
    conn = get_conn(_db_path)
    lim = max(1, min(limit, 500))
    rows = conn.execute(
        "SELECT id, ts, action, detail_json FROM audit ORDER BY ts DESC LIMIT ?",
        (lim,),
    ).fetchall()
    events = []
    for r in rows:
        detail = r["detail_json"]
        try:
            import json

            detail_obj = json.loads(detail) if detail else {}
        except Exception:
            detail_obj = {}
        events.append({"id": r["id"], "ts": r["ts"], "action": r["action"], "detail": detail_obj})
    return {"events": events}


# Static UI (owned by frontend worker; mount if present)
_static = WORKSPACE / "app" / "static"
if _static.is_dir():
    @app.get("/", include_in_schema=False)
    def dashboard_home() -> Response:
        return FileResponse(_static / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(_static)), name="assets")
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="static")

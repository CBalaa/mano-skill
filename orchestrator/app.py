import logging
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException

from orchestrator.config import Settings, settings as default_settings
from orchestrator.planner import PlannerOutcome, build_planner
from orchestrator.schemas import (
    ActionModel,
    GoNoResponse,
    SessionCloseResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStatusResponse,
    StepRequest,
    StepResponse,
    StopResponse,
)
from orchestrator.session_store import SessionConflictError, SessionRecord, SessionStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    app_settings = settings or default_settings
    store = SessionStore()
    planner = build_planner(app_settings)

    app = FastAPI(title="mano-skill orchestrator", version="0.1.0")
    app.state.settings = app_settings
    app.state.store = store
    app.state.planner = planner

    @app.get("/healthz")
    def healthz():
        planner_mode = app_settings.planner_mode
        if planner_mode == "auto":
            planner_mode = "openai" if app_settings.openai_enabled else "mock"
        return {
            "ok": True,
            "planner_mode": planner_mode,
            "model": app_settings.openai_model if app_settings.openai_enabled else None,
        }

    @app.post("/v1/sessions", response_model=SessionCreateResponse)
    def create_session(request: SessionCreateRequest):
        try:
            session = store.create_session(request)
        except SessionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Device already has an active session: {exc}",
            ) from exc
        return SessionCreateResponse(session_id=session.session_id)

    @app.get("/v1/sessions/{session_id}", response_model=SessionStatusResponse)
    def get_session_status(session_id: str):
        session = _require_session(store, session_id)
        with session.lock:
            return _status_response(session)

    @app.post("/v1/sessions/{session_id}/step", response_model=StepResponse)
    def step_session(session_id: str, request: StepRequest):
        session = _require_session(store, session_id)
        _ensure_session_open(session)
        store.ingest_tool_results(session, request.tool_results)

        with session.lock:
            if session.stop_requested:
                return _stop_step_response()
            if session.awaiting_confirmation:
                return _call_user_response()
            if not session.last_screenshot_b64:
                return _capture_screenshot_response()

        outcome = planner.plan(session, request.tool_results)

        with session.lock:
            if session.stop_requested:
                return _stop_step_response()
            session.touch()
            if outcome.status == "CALL_USER":
                session.status = "CALL_USER"
                session.awaiting_confirmation = True
            elif outcome.status == "RUNNING":
                session.status = "RUNNING"
                session.step_count += 1
            else:
                session.status = outcome.status
            return _step_response_from_outcome(outcome)

    @app.post("/v1/sessions/{session_id}/go_no", response_model=GoNoResponse)
    def go_no(session_id: str):
        session = _require_session(store, session_id)
        _ensure_session_open(session)
        with session.lock:
            session.awaiting_confirmation = False
            session.status = "RUNNING"
            session.touch()
            return GoNoResponse(
                ok=True,
                session_id=session_id,
                status="RUNNING",
                message="Session resumed",
            )

    @app.post("/v1/sessions/{session_id}/close", response_model=SessionCloseResponse)
    def close_session(session_id: str, skip_eval: bool = False):
        session = _require_session(store, session_id)
        eval_result = _build_eval_result(session, skip_eval=skip_eval)
        closed_session = store.close_session(session_id)
        if not closed_session:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionCloseResponse(
            ok=True,
            session_id=session_id,
            eval_result=eval_result,
        )

    @app.post("/v1/devices/{device_id}/stop", response_model=StopResponse)
    def stop_device(device_id: str):
        session = store.stop_device(device_id)
        if not session:
            return StopResponse(ok=False, message="No active session found")
        return StopResponse(
            ok=True,
            session_id=session.session_id,
            message="Stop requested",
        )

    return app


def _require_session(store: SessionStore, session_id: str) -> SessionRecord:
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _ensure_session_open(session: SessionRecord):
    if session.closed:
        raise HTTPException(status_code=410, detail="Session is closed")


def _status_response(session: SessionRecord) -> SessionStatusResponse:
    public_status = session.status
    if session.stop_requested:
        public_status = "STOP"
    elif session.awaiting_confirmation:
        public_status = "CALL_USER"
    return SessionStatusResponse(
        session_id=session.session_id,
        device_id=session.device_id,
        status=public_status,
        awaiting_confirmation=session.awaiting_confirmation,
        stop_requested=session.stop_requested,
        closed=session.closed,
        has_screenshot=bool(session.last_screenshot_b64),
        task=session.task,
    )


def _step_response_from_outcome(outcome: PlannerOutcome) -> StepResponse:
    actions = []
    for action in outcome.actions:
        actions.append(
            ActionModel(
                id=f"toolu_{uuid.uuid4().hex[:12]}",
                name=action["name"],
                input=action.get("input") or {},
            )
        )
    return StepResponse(
        status=outcome.status,
        reasoning=outcome.reasoning,
        action_desc=outcome.action_desc,
        actions=actions,
    )


def _capture_screenshot_response() -> StepResponse:
    return StepResponse(
        status="RUNNING",
        reasoning="No screenshot is attached to this session yet, so the client must upload one first.",
        action_desc="Capture a fresh screenshot",
        actions=[
            ActionModel(
                id=f"toolu_{uuid.uuid4().hex[:12]}",
                name="computer",
                input={"action": "screenshot"},
            )
        ],
    )


def _call_user_response() -> StepResponse:
    return StepResponse(
        status="CALL_USER",
        reasoning="The planner requires explicit user confirmation before continuing.",
        action_desc="Waiting for user confirmation",
        actions=[],
    )


def _stop_step_response() -> StepResponse:
    return StepResponse(
        status="STOP",
        reasoning="A stop request is pending for this device.",
        action_desc="Stopping session",
        actions=[],
    )


def _build_eval_result(session: SessionRecord, skip_eval: bool) -> dict:
    if skip_eval:
        return {"status": "skipped", "reason": "skip_eval=true"}
    if not session.expected_result:
        return {"status": "not_requested", "reason": "No expected_result was provided"}
    return {
        "status": "not_implemented",
        "reason": "Evaluation is not implemented in this first local orchestrator version",
        "expected_result": session.expected_result,
        "has_screenshot": bool(session.last_screenshot_b64),
    }


app = create_app()

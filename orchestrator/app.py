import logging
import base64
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Response

from orchestrator.config import Settings, settings as default_settings
from orchestrator.planner import PlannerOutcome, build_planner
from orchestrator.schemas import (
    ActionModel,
    EnqueueActionsRequest,
    EnqueueActionsResponse,
    FinishSessionResponse,
    GoNoResponse,
    LatestScreenshotResponse,
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

    @app.get("/v1/sessions/{session_id}/latest_screenshot", response_model=LatestScreenshotResponse)
    def get_latest_screenshot(session_id: str):
        session = _require_session(store, session_id)
        with session.lock:
            return LatestScreenshotResponse(
                session_id=session.session_id,
                available=bool(session.last_screenshot_b64),
                screenshot_b64=session.last_screenshot_b64,
                updated_at=session.last_screenshot_at.isoformat() if session.last_screenshot_at else None,
            )

    @app.get("/v1/sessions/{session_id}/latest_screenshot.png")
    def get_latest_screenshot_png(session_id: str):
        session = _require_session(store, session_id)
        with session.lock:
            if not session.last_screenshot_b64:
                raise HTTPException(status_code=404, detail="No screenshot available")
            try:
                png_bytes = base64.b64decode(session.last_screenshot_b64)
            except Exception as exc:
                raise HTTPException(status_code=500, detail="Stored screenshot is invalid") from exc
        return Response(content=png_bytes, media_type="image/png")

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
            manual_response = _manual_step_response(session)
            if manual_response:
                return manual_response

        try:
            outcome = app.state.planner.plan(session, request.tool_results)
        except Exception as exc:
            logger.exception("Planner step failed for session %s: %s", session_id, exc)
            outcome = PlannerOutcome(
                status="FAIL",
                reasoning=f"Planner failed: {type(exc).__name__}: {exc}",
                action_desc="Planner request failed",
                actions=[],
            )

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

    @app.post("/v1/sessions/{session_id}/enqueue_actions", response_model=EnqueueActionsResponse)
    def enqueue_actions(session_id: str, request: EnqueueActionsRequest):
        session = _require_session(store, session_id)
        _ensure_session_open(session)
        queued_actions = [action.model_dump() for action in request.actions]
        session = store.enqueue_actions(
            session_id=session_id,
            actions=queued_actions,
            replace_queue=request.replace_queue,
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        with session.lock:
            return EnqueueActionsResponse(
                ok=True,
                session_id=session_id,
                enqueued_actions=len(queued_actions),
                pending_action_batches=len(session.manual_action_batches),
            )

    @app.post("/v1/sessions/{session_id}/finish", response_model=FinishSessionResponse)
    def finish_session(session_id: str):
        session = _require_session(store, session_id)
        _ensure_session_open(session)
        session = store.finish_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return FinishSessionResponse(ok=True, session_id=session_id)

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
        manual_mode=session.manual_mode,
        pending_action_batches=len(session.manual_action_batches),
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


def _manual_step_response(session: SessionRecord) -> Optional[StepResponse]:
    if not session.manual_mode:
        return None

    if session.manual_done:
        session.status = "DONE"
        session.touch()
        return StepResponse(
            status="DONE",
            reasoning="The manual operator marked this session as complete.",
            action_desc="Manual session finished",
            actions=[],
        )

    if session.manual_action_batches:
        actions = session.manual_action_batches.pop(0)
        session.status = "RUNNING"
        session.step_count += 1
        session.touch()
        return _step_response_from_outcome(
            PlannerOutcome(
                status="RUNNING",
                reasoning="Dispatching a manually queued action batch.",
                action_desc="Execute manually queued actions",
                actions=actions,
            )
        )

    session.status = "RUNNING"
    session.touch()
    return StepResponse(
        status="RUNNING",
        reasoning="Manual mode is enabled and the orchestrator is waiting for queued actions.",
        action_desc="Wait for manual actions",
        actions=[
            ActionModel(
                id=f"toolu_{uuid.uuid4().hex[:12]}",
                name="computer",
                input={"action": "wait"},
            )
        ],
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

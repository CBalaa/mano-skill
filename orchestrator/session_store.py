import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from orchestrator.schemas import SessionCreateRequest, ToolResult


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionRecord:
    session_id: str
    device_id: str
    platform: str
    task: str
    expected_result: Optional[str]
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    last_screenshot_b64: Optional[str] = None
    last_tool_results: List[Dict[str, Any]] = field(default_factory=list)
    planner_state: Dict[str, Any] = field(default_factory=dict)
    status: str = "RUNNING"
    awaiting_confirmation: bool = False
    stop_requested: bool = False
    closed: bool = False
    step_count: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def touch(self):
        self.updated_at = utc_now()


class SessionConflictError(RuntimeError):
    pass


class SessionStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, SessionRecord] = {}
        self._active_by_device: Dict[str, str] = {}

    def create_session(self, request: SessionCreateRequest) -> SessionRecord:
        with self._lock:
            active_session_id = self._active_by_device.get(request.device_id)
            if active_session_id:
                active_session = self._sessions.get(active_session_id)
                if active_session and not active_session.closed:
                    raise SessionConflictError(active_session_id)

            session_id = str(uuid.uuid4())
            session = SessionRecord(
                session_id=session_id,
                device_id=request.device_id,
                platform=request.platform,
                task=request.task,
                expected_result=request.expected_result,
            )
            self._sessions[session_id] = session
            self._active_by_device[request.device_id] = session_id
            return session

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            with session.lock:
                session.closed = True
                session.status = "CLOSED"
                session.touch()
            active_id = self._active_by_device.get(session.device_id)
            if active_id == session_id:
                del self._active_by_device[session.device_id]
            return session

    def stop_device(self, device_id: str) -> Optional[SessionRecord]:
        with self._lock:
            session_id = self._active_by_device.get(device_id)
            if not session_id:
                return None
            session = self._sessions.get(session_id)
            if not session:
                return None
            with session.lock:
                session.stop_requested = True
                session.status = "STOP"
                session.touch()
            del self._active_by_device[device_id]
            return session

    def ingest_tool_results(self, session: SessionRecord, tool_results: List[ToolResult]):
        if not tool_results:
            return

        with session.lock:
            session.last_tool_results = [item.model_dump() for item in tool_results]
            for item in tool_results:
                if item.include_screenshot and item.screenshot_b64:
                    session.last_screenshot_b64 = item.screenshot_b64
                    break
            session.touch()

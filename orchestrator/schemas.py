from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    task: str
    device_id: str
    platform: str
    expected_result: Optional[str] = None


class SessionCreateResponse(BaseModel):
    session_id: str
    status: str = "RUNNING"


class ToolResult(BaseModel):
    tool_use_id: str
    status: str
    output: Optional[str] = None
    error: Optional[str] = None
    include_screenshot: bool = False
    screenshot_b64: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class StepRequest(BaseModel):
    request_id: str
    tool_results: List[ToolResult] = Field(default_factory=list)


class ActionModel(BaseModel):
    id: str
    name: str
    input: Dict[str, Any] = Field(default_factory=dict)


class QueuedActionModel(BaseModel):
    name: str
    input: Dict[str, Any] = Field(default_factory=dict)


class StepResponse(BaseModel):
    status: str
    reasoning: str = ""
    action_desc: str = ""
    actions: List[ActionModel] = Field(default_factory=list)


class SessionCloseResponse(BaseModel):
    ok: bool
    session_id: str
    eval_result: Dict[str, Any] = Field(default_factory=dict)


class StopResponse(BaseModel):
    ok: bool
    session_id: Optional[str] = None
    message: str = ""


class GoNoResponse(BaseModel):
    ok: bool
    session_id: str
    status: str
    message: str = ""


class SessionStatusResponse(BaseModel):
    session_id: str
    device_id: str
    status: str
    awaiting_confirmation: bool = False
    stop_requested: bool = False
    closed: bool = False
    manual_mode: bool = False
    pending_action_batches: int = 0
    has_screenshot: bool = False
    task: str


class EnqueueActionsRequest(BaseModel):
    actions: List[QueuedActionModel] = Field(default_factory=list)
    replace_queue: bool = False


class EnqueueActionsResponse(BaseModel):
    ok: bool
    session_id: str
    manual_mode: bool = True
    enqueued_actions: int = 0
    pending_action_batches: int = 0


class LatestScreenshotResponse(BaseModel):
    session_id: str
    available: bool = False
    screenshot_b64: Optional[str] = None
    updated_at: Optional[str] = None


class FinishSessionResponse(BaseModel):
    ok: bool
    session_id: str
    status: str = "DONE"

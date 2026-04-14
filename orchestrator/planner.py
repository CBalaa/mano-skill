import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import requests
from orchestrator.config import Settings
from orchestrator.schemas import ToolResult
from orchestrator.session_store import SessionRecord

logger = logging.getLogger(__name__)


PLANNER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["RUNNING", "DONE", "FAIL", "CALL_USER"],
        },
        "reasoning": {"type": "string"},
        "action_desc": {"type": "string"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["computer", "open_app", "open_url", "minimize_panel"],
                    },
                    "input_json": {"type": "string"},
                },
                "required": ["name", "input_json"],
            },
        },
    },
    "required": ["status", "reasoning", "action_desc", "actions"],
}


PLANNER_SYSTEM_PROMPT = """You are the planner for a desktop automation orchestrator.

Decide exactly one next step based on the latest screenshot and recent tool results.

Rules:
- Return status RUNNING, DONE, FAIL, or CALL_USER.
- When status is RUNNING, return 1-3 actions. When status is DONE, FAIL, or CALL_USER, return an empty actions list.
- If you include any action at all, status MUST be RUNNING.
- Prefer the existing action protocol. Common actions use name="computer" with input.action in:
  left_click, double_click, right_click, mouse_move, type, key, scroll, left_click_drag, wait, screenshot, done, fail, call_user.
- open_app and open_url use their own tool names.
- Each action must return `input_json` as a compact JSON object string, for example `{"action":"wait"}`.
- Coordinates must target the 1280x720 screenshot space.
- Use CALL_USER for destructive, irreversible, credential, payment, or ambiguous operations.
- Keep reasoning concise and factual.
- The screenshot is the source of truth. Do not infer that an app or website is already open just because terminal text or logs mention it.
- If the task says to open a browser or visit a URL and that page is not visibly open yet, prefer a single open_url action instead of waiting.
- If the task says to open an app and the app is not visibly open yet, prefer open_app instead of waiting.
- Do not ask for another screenshot unless you genuinely need a fresh one after a visible state change.
"""


SENSITIVE_PATTERNS = [
    r"\bdelete\b",
    r"\bremove\b",
    r"\bpayment\b",
    r"\bpay\b",
    r"\btransfer\b",
    r"\bpurchase\b",
    r"\bcheckout\b",
    r"\bpassword\b",
    r"\blogin\b",
    r"\bconfirm\b",
    r"删除",
    r"付款",
    r"支付",
    r"转账",
    r"密码",
    r"确认",
]


@dataclass
class PlannerOutcome:
    status: str
    reasoning: str
    action_desc: str
    actions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamedResponsesResult:
    text: str
    response_id: Optional[str] = None


@dataclass
class SSEEvent:
    event: Optional[str]
    data_lines: List[str] = field(default_factory=list)


class BasePlanner:
    def plan(self, session: SessionRecord, tool_results: List[ToolResult]) -> PlannerOutcome:
        raise NotImplementedError


class MockPlanner(BasePlanner):
    def plan(self, session: SessionRecord, tool_results: List[ToolResult]) -> PlannerOutcome:
        task = session.task.lower()
        phase = session.planner_state.get("mock_phase", "initial")
        if not session.planner_state.get("confirmation_requested") and _looks_sensitive(task):
            session.planner_state["confirmation_requested"] = True
            return PlannerOutcome(
                status="CALL_USER",
                reasoning="The mock planner flagged this task as sensitive and requires approval before continuing.",
                action_desc="Waiting for user confirmation",
                actions=[],
            )

        if phase == "initial":
            session.planner_state["mock_phase"] = "after_wait"
            return PlannerOutcome(
                status="RUNNING",
                reasoning="Mock planner captured the latest screenshot and is issuing a harmless wait step to verify the end-to-end loop.",
                action_desc="Wait briefly so the client can complete one action round-trip",
                actions=[{"name": "computer", "input": {"action": "wait"}}],
            )

        return PlannerOutcome(
            status="DONE",
            reasoning="Mock planner completed after a single benign action.",
            action_desc="Task completed by mock planner",
            actions=[],
        )


class OpenAIPlanner(BasePlanner):
    def __init__(self, settings: Settings):
        self._settings = settings

    def plan(self, session: SessionRecord, tool_results: List[ToolResult]) -> PlannerOutcome:
        if not session.last_screenshot_b64:
            return PlannerOutcome(
                status="RUNNING",
                reasoning="The planner needs a screenshot before it can decide on the next action.",
                action_desc="Capture screenshot",
                actions=[{"name": "computer", "input": {"action": "screenshot"}}],
            )

        user_text = _build_user_prompt(session, tool_results)
        try:
            return self._plan_with_responses_api(session, user_text)
        except Exception as exc:
            if not self._settings.public_base_url:
                raise
            logger.warning("Responses API planner failed, trying chat.completions vision fallback: %s", exc)
            return self._plan_with_chat_completions_vision(session, user_text)

    def _plan_with_responses_api(self, session: SessionRecord, user_text: str) -> PlannerOutcome:
        payload = _build_responses_payload(
            model=self._settings.openai_model,
            reasoning_effort=self._settings.openai_reasoning_effort,
            user_text=user_text,
            screenshot_b64=session.last_screenshot_b64,
        )
        result = _stream_responses_request(
            base_url=self._settings.openai_base_url,
            api_key=self._settings.openai_api_key,
            timeout=self._settings.openai_timeout,
            payload=payload,
        )
        payload = json.loads(result.text)
        session.planner_state["last_openai_response_id"] = result.response_id
        session.planner_state["planner_backend"] = "responses"
        outcome = _normalize_outcome(payload)
        _record_planner_outcome(session, outcome)
        return outcome

    def _plan_with_chat_completions_vision(self, session: SessionRecord, user_text: str) -> PlannerOutcome:
        if not self._settings.openai_base_url:
            raise RuntimeError("Vision fallback requires MANO_OPENAI_BASE_URL or OPENAI_BASE_URL")

        screenshot_url = (
            f"{self._settings.public_base_url}/v1/sessions/{session.session_id}/latest_screenshot.png"
        )
        response_text = _stream_chat_completion(
            base_url=self._settings.openai_base_url,
            api_key=self._settings.openai_api_key,
            model=self._settings.openai_model,
            timeout=self._settings.openai_timeout,
            messages=[
                {
                    "role": "system",
                    "content": (
                        PLANNER_SYSTEM_PROMPT
                        + "\nReturn only JSON that matches the provided schema."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": screenshot_url}},
                    ],
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "planner_response",
                    "schema": PLANNER_OUTPUT_SCHEMA,
                    "strict": True,
                },
            },
        )
        payload = json.loads(response_text)
        session.planner_state["planner_backend"] = "chat_completions_vision"
        session.planner_state["last_screenshot_url"] = screenshot_url
        outcome = _normalize_outcome(payload)
        _record_planner_outcome(session, outcome)
        return outcome


def build_planner(settings: Settings) -> BasePlanner:
    mock = MockPlanner()
    if settings.planner_mode == "mock":
        return mock
    if settings.planner_mode == "openai" and not settings.openai_enabled:
        logger.warning("MANO_PLANNER_MODE=openai but OPENAI_API_KEY is unset, falling back to mock planner.")
        return mock
    if settings.planner_mode == "openai" or settings.openai_enabled:
        try:
            return OpenAIPlanner(settings)
        except Exception as exc:
            logger.warning("OpenAI planner unavailable, using mock planner instead: %s", exc)
            return mock
    return mock


def _build_user_prompt(session: SessionRecord, tool_results: List[ToolResult]) -> str:
    recent_results = tool_results or []
    result_lines = []
    for item in recent_results[-5:]:
        result_lines.append(
            json.dumps(
                {
                    "tool_use_id": item.tool_use_id,
                    "status": item.status,
                    "output": item.output,
                    "error": item.error,
                    "meta": item.meta,
                },
                ensure_ascii=False,
            )
        )

    return "\n".join(
        [
            f"Task: {session.task}",
            f"Expected result: {session.expected_result or 'N/A'}",
            f"Platform: {session.platform}",
            f"Step count: {session.step_count}",
            "Recent tool results:",
            "\n".join(result_lines) if result_lines else "none",
        ]
    )


def _looks_sensitive(task: str) -> bool:
    return any(re.search(pattern, task, re.IGNORECASE) for pattern in SENSITIVE_PATTERNS)


def _normalize_outcome(payload: Dict[str, Any]) -> PlannerOutcome:
    status = str(payload.get("status", "FAIL")).upper()
    reasoning = str(payload.get("reasoning", "")).strip()
    action_desc = str(payload.get("action_desc", "")).strip()
    actions = payload.get("actions") or []
    if status not in {"RUNNING", "DONE", "FAIL", "CALL_USER"}:
        status = "FAIL"
        reasoning = reasoning or "Planner returned an invalid status."
        action_desc = action_desc or "Planner failed"
        actions = []

    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = str(action.get("name") or "").strip()
        action_input = action.get("input")
        if not isinstance(action_input, dict):
            action_input = {}
        input_json = action.get("input_json")
        if isinstance(input_json, str) and input_json.strip():
            try:
                parsed_input = json.loads(input_json)
            except json.JSONDecodeError:
                parsed_input = {}
            if isinstance(parsed_input, dict):
                action_input = parsed_input
        if name not in {"computer", "open_app", "open_url", "minimize_panel"}:
            continue
        if name == "computer":
            action_name = str(action_input.get("action") or "").strip()
            if action_name not in {
                "left_click",
                "double_click",
                "right_click",
                "mouse_move",
                "type",
                "key",
                "scroll",
                "left_click_drag",
                "wait",
                "screenshot",
                "done",
                "fail",
                "call_user",
            }:
                continue
        elif name == "open_app":
            if not str(action_input.get("app_name") or "").strip():
                continue
        elif name == "open_url":
            if not str(action_input.get("url") or "").strip():
                continue
        normalized_actions.append({"name": name, "input": action_input})

    if status != "RUNNING" and normalized_actions:
        logger.warning(
            "Planner returned status=%s with %d valid actions; coercing status to RUNNING",
            status,
            len(normalized_actions),
        )
        status = "RUNNING"

    if status == "RUNNING" and not normalized_actions:
        normalized_actions = [{"name": "computer", "input": {"action": "wait"}}]
        if not reasoning:
            reasoning = "Planner returned no valid actions, so the orchestrator inserted a wait step."
        if not action_desc:
            action_desc = "Wait briefly"

    return PlannerOutcome(
        status=status,
        reasoning=reasoning,
        action_desc=action_desc,
        actions=normalized_actions,
    )


def _record_planner_outcome(session: SessionRecord, outcome: PlannerOutcome):
    session.planner_state["last_planner_status"] = outcome.status
    session.planner_state["last_planner_reasoning"] = outcome.reasoning
    session.planner_state["last_planner_action_desc"] = outcome.action_desc
    session.planner_state["last_planner_actions"] = outcome.actions
    logger.info(
        "Planner outcome session=%s backend=%s status=%s actions=%d action_desc=%s reasoning=%s",
        session.session_id,
        session.planner_state.get("planner_backend", "unknown"),
        outcome.status,
        len(outcome.actions),
        outcome.action_desc,
        outcome.reasoning,
    )


def _stream_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
    messages: List[Dict[str, Any]],
    response_format: Dict[str, Any],
) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "stream": True,
        "messages": messages,
        "response_format": response_format,
    }
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    response.encoding = "utf-8"

    parts: List[str] = []
    for sse_event in _iter_sse_events(response.iter_lines(decode_unicode=True)):
        data = _coalesce_sse_data(sse_event.data_lines)
        if not data or data == "[DONE]":
            break
        chunk = _loads_sse_json(sse_event)
        error = chunk.get("error")
        if error:
            raise RuntimeError(error.get("message") or "chat.completions returned an error")
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                parts.append(content)

    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("chat.completions returned no content")
    return text


def _build_responses_payload(
    *,
    model: str,
    reasoning_effort: str,
    user_text: str,
    screenshot_b64: str,
) -> Dict[str, Any]:
    return {
        "model": model,
        "instructions": PLANNER_SYSTEM_PROMPT,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "<image>"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{screenshot_b64}",
                    },
                    {"type": "input_text", "text": "</image>"},
                    {"type": "input_text", "text": user_text},
                ],
            }
        ],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": reasoning_effort},
        "store": False,
        "stream": True,
        "include": [],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "planner_response",
                "schema": PLANNER_OUTPUT_SCHEMA,
                "strict": True,
            }
        },
    }


def _default_responses_base_url(base_url: str) -> str:
    return (base_url or "https://api.openai.com/v1").rstrip("/")


def _stream_responses_request(
    *,
    base_url: str,
    api_key: str,
    timeout: float,
    payload: Dict[str, Any],
) -> StreamedResponsesResult:
    url = f"{_default_responses_base_url(base_url)}/responses"
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json=payload,
        timeout=timeout,
        stream=True,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    return _parse_responses_stream(response.iter_lines(decode_unicode=True))


def _parse_responses_stream(lines: Iterable[str]) -> StreamedResponsesResult:
    response_id = None
    parts: List[str] = []

    for sse_event in _iter_sse_events(lines):
        data = _coalesce_sse_data(sse_event.data_lines)
        if not data or data == "[DONE]":
            continue

        chunk = _loads_sse_json(sse_event)
        error = chunk.get("error")
        if error:
            raise RuntimeError(error.get("message") or "responses API returned an error")

        response_obj = chunk.get("response")
        if isinstance(response_obj, dict):
            response_id = response_obj.get("id") or response_id
            response_error = response_obj.get("error")
            if response_error:
                if isinstance(response_error, dict):
                    raise RuntimeError(response_error.get("message") or "responses API returned an error")
                raise RuntimeError(str(response_error))

        chunk_type = chunk.get("type")
        if chunk_type == "response.output_text.delta":
            delta = chunk.get("delta")
            if delta:
                parts.append(delta)

    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("responses API returned no output text")
    return StreamedResponsesResult(text=text, response_id=response_id)


def _iter_sse_events(lines: Iterable[str]) -> Iterable[SSEEvent]:
    event_name: Optional[str] = None
    data_lines: List[str] = []

    for raw_line in lines:
        if raw_line is None:
            continue
        line = raw_line.rstrip("\r\n")

        if line == "":
            if event_name is not None or data_lines:
                yield SSEEvent(event=event_name, data_lines=data_lines)
            event_name = None
            data_lines = []
            continue

        if line.startswith(":"):
            continue

        field, sep, value = line.partition(":")
        if not sep:
            if data_lines:
                data_lines.append(line)
            continue
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            if event_name is not None or data_lines:
                yield SSEEvent(event=event_name, data_lines=data_lines)
                data_lines = []
            event_name = value
        elif field == "data":
            data_lines.append(value)

    if event_name is not None or data_lines:
        yield SSEEvent(event=event_name, data_lines=data_lines)


def _coalesce_sse_data(data_lines: List[str]) -> str:
    if not data_lines:
        return ""
    return "\n".join(data_lines).strip()


def _loads_sse_json(sse_event: SSEEvent) -> Dict[str, Any]:
    candidates: List[str] = []
    joined_with_newlines = _coalesce_sse_data(sse_event.data_lines)
    if joined_with_newlines:
        candidates.append(joined_with_newlines)

    joined_without_newlines = "".join(sse_event.data_lines).strip()
    if joined_without_newlines and joined_without_newlines not in candidates:
        candidates.append(joined_without_newlines)

    last_error: Optional[Exception] = None
    for candidate in candidates:
        if candidate == "[DONE]":
            return {"type": "[DONE]"}
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, dict):
            return payload
        raise RuntimeError("responses stream yielded a non-object JSON payload")

    preview = (candidates[-1] if candidates else "")[:200]
    raise RuntimeError(
        f"Failed to decode SSE JSON event {sse_event.event or '<unknown>'}: {last_error}. Preview: {preview!r}"
    )

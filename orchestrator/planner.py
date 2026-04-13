import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

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
                    "input": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["name", "input"],
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
- Prefer the existing action protocol. Common actions use name="computer" with input.action in:
  left_click, double_click, right_click, mouse_move, type, key, scroll, left_click_drag, wait, screenshot, done, fail, call_user.
- open_app and open_url use their own tool names.
- Coordinates must target the 1280x720 screenshot space.
- Use CALL_USER for destructive, irreversible, credential, payment, or ambiguous operations.
- Keep reasoning concise and factual.
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
        from openai import OpenAI

        self._settings = settings
        client_kwargs = {
            "api_key": settings.openai_api_key,
            "timeout": settings.openai_timeout,
        }
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url
        self._client = OpenAI(**client_kwargs)

    def plan(self, session: SessionRecord, tool_results: List[ToolResult]) -> PlannerOutcome:
        if not session.last_screenshot_b64:
            return PlannerOutcome(
                status="RUNNING",
                reasoning="The planner needs a screenshot before it can decide on the next action.",
                action_desc="Capture screenshot",
                actions=[{"name": "computer", "input": {"action": "screenshot"}}],
            )

        user_text = _build_user_prompt(session, tool_results)
        response = self._client.responses.create(
            model=self._settings.openai_model,
            reasoning={"effort": self._settings.openai_reasoning_effort},
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": PLANNER_SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": user_text},
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{session.last_screenshot_b64}",
                        },
                    ],
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "planner_response",
                    "schema": PLANNER_OUTPUT_SCHEMA,
                    "strict": True,
                }
            },
        )
        output_text = getattr(response, "output_text", "") or ""
        if not output_text:
            raise RuntimeError("Responses API returned no output_text")

        payload = json.loads(output_text)
        session.planner_state["last_openai_response_id"] = getattr(response, "id", None)
        return _normalize_outcome(payload)


class FallbackPlanner(BasePlanner):
    def __init__(self, primary: BasePlanner, fallback: BasePlanner):
        self._primary = primary
        self._fallback = fallback

    def plan(self, session: SessionRecord, tool_results: List[ToolResult]) -> PlannerOutcome:
        try:
            return self._primary.plan(session, tool_results)
        except Exception as exc:
            logger.exception("Primary planner failed, falling back to mock planner: %s", exc)
            return self._fallback.plan(session, tool_results)


def build_planner(settings: Settings) -> BasePlanner:
    mock = MockPlanner()
    if settings.planner_mode == "mock":
        return mock
    if settings.planner_mode == "openai" and not settings.openai_enabled:
        logger.warning("MANO_PLANNER_MODE=openai but OPENAI_API_KEY is unset, falling back to mock planner.")
        return mock
    if settings.planner_mode == "openai" or settings.openai_enabled:
        try:
            primary = OpenAIPlanner(settings)
        except Exception as exc:
            logger.warning("OpenAI planner unavailable, using mock planner instead: %s", exc)
            return mock
        return FallbackPlanner(primary, mock)
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

    if status != "RUNNING":
        actions = []

    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = str(action.get("name") or "").strip()
        action_input = action.get("input")
        if not isinstance(action_input, dict):
            action_input = {}
        if name not in {"computer", "open_app", "open_url", "minimize_panel"}:
            continue
        normalized_actions.append({"name": name, "input": action_input})

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

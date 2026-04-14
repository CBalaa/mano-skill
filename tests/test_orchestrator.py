import unittest

from fastapi.testclient import TestClient

from orchestrator.app import create_app
from orchestrator.config import Settings
from orchestrator.planner import (
    PLANNER_SYSTEM_PROMPT,
    _build_responses_payload,
    build_planner,
    _normalize_outcome,
    _parse_responses_stream,
)


def build_test_client():
    app = create_app(
        Settings(
            host="127.0.0.1",
            port=8000,
            planner_mode="mock",
            openai_api_key="",
            openai_base_url="",
            public_base_url="",
            openai_model="gpt-5.4",
            openai_reasoning_effort="medium",
            openai_timeout=30.0,
        )
    )
    return TestClient(app)


class OrchestratorTests(unittest.TestCase):
    def test_responses_payload_matches_codex_image_shape(self):
        payload = _build_responses_payload(
            model="gpt-5.4",
            reasoning_effort="medium",
            user_text="Task: click the button",
            screenshot_b64="QUJD",
        )

        self.assertEqual(payload["instructions"], PLANNER_SYSTEM_PROMPT)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        content = payload["input"][0]["content"]
        self.assertEqual(content[0]["text"], "<image>")
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["image_url"], "data:image/png;base64,QUJD")
        self.assertEqual(content[2]["text"], "</image>")
        self.assertEqual(content[3]["text"], "Task: click the button")

    def test_parse_responses_stream_collects_text_and_response_id(self):
        result = _parse_responses_stream(
            [
                'event: response.created',
                'data: {"type":"response.created","response":{"id":"resp_123","error":null}}',
                "",
                'event: response.output_text.delta',
                'data: {"type":"response.output_text.delta","delta":"{\\"status\\":"}',
                "",
                'event: response.output_text.delta',
                'data: {"type":"response.output_text.delta","delta":"\\"DONE\\"}"}',
                "",
                'event: response.completed',
                'data: {"type":"response.completed","response":{"id":"resp_123","error":null}}',
            ]
        )

        self.assertEqual(result.response_id, "resp_123")
        self.assertEqual(result.text, '{"status":"DONE"}')

    def test_parse_responses_stream_handles_split_json_across_data_lines(self):
        result = _parse_responses_stream(
            [
                "event: response.created",
                'data: {"type":"response.created","response":{"id":"resp_456","error":null}}',
                "",
                "event: response.output_text.delta",
                'data: {"type":"response.output_text.delta","delta":"{\\"st',
                'data: atus\\":\\"RUNNING\\"}"}',
                "",
                "event: response.completed",
                'data: {"type":"response.completed","response":{"id":"resp_456","error":null}}',
            ]
        )

        self.assertEqual(result.response_id, "resp_456")
        self.assertEqual(result.text, '{"status":"RUNNING"}')

    def test_normalize_outcome_parses_input_json(self):
        outcome = _normalize_outcome(
            {
                "status": "RUNNING",
                "reasoning": "continue",
                "action_desc": "wait",
                "actions": [
                    {
                        "name": "computer",
                        "input_json": '{"action":"wait"}',
                    }
                ],
            }
        )

        self.assertEqual(outcome.status, "RUNNING")
        self.assertEqual(outcome.actions, [{"name": "computer", "input": {"action": "wait"}}])

    def test_normalize_outcome_coerces_done_with_actions_to_running(self):
        outcome = _normalize_outcome(
            {
                "status": "DONE",
                "reasoning": "Open the website now.",
                "action_desc": "Open browser",
                "actions": [
                    {"name": "computer", "input_json": "{}"},
                    {"name": "open_url", "input_json": '{"url":"https://example.com"}'},
                ],
            }
        )

        self.assertEqual(outcome.status, "RUNNING")
        self.assertEqual(outcome.actions, [{"name": "open_url", "input": {"url": "https://example.com"}}])

    def test_build_planner_openai_mode_uses_real_planner(self):
        planner = build_planner(
            Settings(
                host="127.0.0.1",
                port=8000,
                planner_mode="openai",
                openai_api_key="test-key",
                openai_base_url="https://example.invalid/v1",
                public_base_url="",
                openai_model="gpt-5.4",
                openai_reasoning_effort="low",
                openai_timeout=30.0,
            )
        )

        self.assertEqual(type(planner).__name__, "OpenAIPlanner")

    def test_step_bootstraps_with_screenshot_then_finishes(self):
        client = build_test_client()
        created = client.post(
            "/v1/sessions",
            json={
                "task": "Observe the desktop and finish cleanly",
                "device_id": "device-a",
                "platform": "Linux",
            },
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session_id"]

        first_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "req-1", "tool_results": []},
        )
        self.assertEqual(first_step.status_code, 200)
        self.assertEqual(first_step.json()["actions"][0]["input"]["action"], "screenshot")

        second_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={
                "request_id": "req-2",
                "tool_results": [
                    {
                        "tool_use_id": "toolu_1",
                        "status": "success",
                        "output": "screenshot requested",
                        "include_screenshot": True,
                        "screenshot_b64": "ZmFrZQ==",
                        "meta": {"action": "screenshot"},
                    }
                ],
            },
        )
        self.assertEqual(second_step.status_code, 200)
        self.assertEqual(second_step.json()["status"], "RUNNING")
        self.assertEqual(second_step.json()["actions"][0]["input"]["action"], "wait")

        third_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={
                "request_id": "req-3",
                "tool_results": [
                    {
                        "tool_use_id": "toolu_2",
                        "status": "success",
                        "output": "wait ok",
                        "include_screenshot": True,
                        "screenshot_b64": "ZmFrZQ==",
                        "meta": {"action": "wait"},
                    }
                ],
            },
        )
        self.assertEqual(third_step.status_code, 200)
        self.assertEqual(third_step.json()["status"], "DONE")

    def test_call_user_and_go_no(self):
        client = build_test_client()
        created = client.post(
            "/v1/sessions",
            json={
                "task": "Confirm payment in the app",
                "device_id": "device-b",
                "platform": "Linux",
            },
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session_id"]

        client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "req-1", "tool_results": []},
        )
        pause_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={
                "request_id": "req-2",
                "tool_results": [
                    {
                        "tool_use_id": "toolu_1",
                        "status": "success",
                        "output": "screenshot requested",
                        "include_screenshot": True,
                        "screenshot_b64": "ZmFrZQ==",
                        "meta": {"action": "screenshot"},
                    }
                ],
            },
        )
        self.assertEqual(pause_step.status_code, 200)
        self.assertEqual(pause_step.json()["status"], "CALL_USER")

        resumed = client.post(f"/v1/sessions/{session_id}/go_no")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "RUNNING")

        next_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "req-3", "tool_results": []},
        )
        self.assertEqual(next_step.status_code, 200)
        self.assertEqual(next_step.json()["status"], "RUNNING")

    def test_stop_endpoint_marks_active_session(self):
        client = build_test_client()
        created = client.post(
            "/v1/sessions",
            json={
                "task": "Observe the desktop and finish cleanly",
                "device_id": "device-c",
                "platform": "Linux",
            },
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session_id"]

        stopped = client.post("/v1/devices/device-c/stop")
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(stopped.json()["ok"])
        self.assertEqual(stopped.json()["session_id"], session_id)

        step_after_stop = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "req-1", "tool_results": []},
        )
        self.assertEqual(step_after_stop.status_code, 200)
        self.assertEqual(step_after_stop.json()["status"], "STOP")

        recreated = client.post(
            "/v1/sessions",
            json={
                "task": "Start a replacement session",
                "device_id": "device-c",
                "platform": "Linux",
            },
        )
        self.assertEqual(recreated.status_code, 200)

    def test_closed_session_rejects_future_step_and_go_no(self):
        client = build_test_client()
        created = client.post(
            "/v1/sessions",
            json={
                "task": "Observe the desktop and finish cleanly",
                "device_id": "device-d",
                "platform": "Linux",
            },
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session_id"]

        closed = client.post(f"/v1/sessions/{session_id}/close")
        self.assertEqual(closed.status_code, 200)

        step_after_close = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "req-closed", "tool_results": []},
        )
        self.assertEqual(step_after_close.status_code, 410)

        go_no_after_close = client.post(f"/v1/sessions/{session_id}/go_no")
        self.assertEqual(go_no_after_close.status_code, 410)

    def test_step_returns_fail_when_planner_raises(self):
        client = build_test_client()

        class BrokenPlanner:
            def plan(self, session, tool_results):
                raise RuntimeError("boom")

        client.app.state.planner = BrokenPlanner()

        created = client.post(
            "/v1/sessions",
            json={
                "task": "Open a website",
                "device_id": "device-broken-planner",
                "platform": "Windows",
            },
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session_id"]

        first_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "req-1", "tool_results": []},
        )
        self.assertEqual(first_step.status_code, 200)
        self.assertEqual(first_step.json()["actions"][0]["input"]["action"], "screenshot")

        second_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={
                "request_id": "req-2",
                "tool_results": [
                    {
                        "tool_use_id": "toolu_1",
                        "status": "success",
                        "output": "screenshot requested",
                        "include_screenshot": True,
                        "screenshot_b64": "ZmFrZQ==",
                        "meta": {"action": "screenshot"},
                    }
                ],
            },
        )
        self.assertEqual(second_step.status_code, 200)
        self.assertEqual(second_step.json()["status"], "FAIL")
        self.assertIn("Planner failed", second_step.json()["reasoning"])

    def test_manual_mode_waits_for_server_actions_and_can_finish(self):
        client = build_test_client()
        created = client.post(
            "/v1/sessions",
            json={
                "task": "[manual] Remote desktop control",
                "device_id": "device-manual",
                "platform": "Windows",
            },
        )
        self.assertEqual(created.status_code, 200)
        session_id = created.json()["session_id"]

        first_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "manual-1", "tool_results": []},
        )
        self.assertEqual(first_step.status_code, 200)
        self.assertEqual(first_step.json()["actions"][0]["input"]["action"], "screenshot")

        wait_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={
                "request_id": "manual-2",
                "tool_results": [
                    {
                        "tool_use_id": "toolu_screenshot",
                        "status": "success",
                        "output": "screenshot requested",
                        "include_screenshot": True,
                        "screenshot_b64": "ZmFrZQ==",
                        "meta": {"action": "screenshot"},
                    }
                ],
            },
        )
        self.assertEqual(wait_step.status_code, 200)
        self.assertEqual(wait_step.json()["actions"][0]["input"]["action"], "wait")

        latest_screenshot = client.get(f"/v1/sessions/{session_id}/latest_screenshot")
        self.assertEqual(latest_screenshot.status_code, 200)
        self.assertTrue(latest_screenshot.json()["available"])
        self.assertEqual(latest_screenshot.json()["screenshot_b64"], "ZmFrZQ==")

        enqueued = client.post(
            f"/v1/sessions/{session_id}/enqueue_actions",
            json={
                "actions": [
                    {
                        "name": "computer",
                        "input": {
                            "action": "key",
                            "mains": ["enter"],
                        },
                    }
                ]
            },
        )
        self.assertEqual(enqueued.status_code, 200)
        self.assertEqual(enqueued.json()["pending_action_batches"], 1)

        status = client.get(f"/v1/sessions/{session_id}")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["manual_mode"])
        self.assertEqual(status.json()["pending_action_batches"], 1)

        action_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "manual-3", "tool_results": []},
        )
        self.assertEqual(action_step.status_code, 200)
        self.assertEqual(action_step.json()["actions"][0]["input"]["action"], "key")

        finished = client.post(f"/v1/sessions/{session_id}/finish")
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(finished.json()["status"], "DONE")

        done_step = client.post(
            f"/v1/sessions/{session_id}/step",
            json={"request_id": "manual-4", "tool_results": []},
        )
        self.assertEqual(done_step.status_code, 200)
        self.assertEqual(done_step.json()["status"], "DONE")


if __name__ == "__main__":
    unittest.main()

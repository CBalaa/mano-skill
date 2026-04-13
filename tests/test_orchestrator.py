import unittest

from fastapi.testclient import TestClient

from orchestrator.app import create_app
from orchestrator.config import Settings


def build_test_client():
    app = create_app(
        Settings(
            host="127.0.0.1",
            port=8000,
            planner_mode="mock",
            openai_api_key="",
            openai_base_url="",
            openai_model="gpt-5.4",
            openai_reasoning_effort="medium",
            openai_timeout=30.0,
        )
    )
    return TestClient(app)


class OrchestratorTests(unittest.TestCase):
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

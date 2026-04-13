#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "requests",
#     "pynput",
#     "mss",
#     "customtkinter",
# ]
# ///

import sys
import platform
import argparse
from pathlib import Path
from typing import Optional

import requests


if __package__ in (None, ""):
    repo_root = Path(__file__).resolve().parent.parent
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)


def _close_session_quietly(server_url: str, session_id: str):
    """Best-effort cleanup for sessions created before local initialization fails."""
    try:
        requests.post(
            f"{server_url}/v1/sessions/{session_id}/close?skip_eval=true",
            json={},
            timeout=10,
        )
    except Exception:
        pass


def stop_session(server_url: Optional[str] = None):
    """Stop the current active session for this device"""
    from visual.config.visual_config import resolve_server_url
    from visual.computer.computer_use_util import get_or_create_device_id
    
    device_id = get_or_create_device_id()
    base_url = resolve_server_url(server_url)
    
    try:
        resp = requests.post(
            f"{base_url}/v1/devices/{device_id}/stop",
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        
        if data.get("ok"):
            print(f"Session stopped: {data.get('session_id')}")
            return 0
        else:
            print(f"No active session: {data.get('message')}")
            return 1
    except Exception as e:
        print(f"Failed to stop session: {e}")
        return 1


def run_task(
    task: str,
    expected_result: str = None,
    minimize: bool = False,
    server_url: Optional[str] = None,
    headless: bool = False,
):
    """Run an automation task"""
    from visual.config.visual_config import AUTOMATION_CONFIG, API_HEADERS, resolve_server_url
    from visual.computer.computer_use_util import get_or_create_device_id
    from visual.view_model.task_view_model import TaskViewModel
    
    # 1. Try to create session BEFORE initializing UI
    device_id = get_or_create_device_id()
    base_url = resolve_server_url(server_url)
    try:
        body = {
            "task": task,
            "device_id": device_id,
            "platform": platform.system()
        }
        if expected_result:
            body["expected_result"] = expected_result
            
        resp = requests.post(
            f"{base_url}/v1/sessions",
            json=body,
            headers=API_HEADERS,
            timeout=AUTOMATION_CONFIG["SESSION_TIMEOUT"]
        )
        if resp.status_code == 409:
            print(f"Error: Another task is already running on this device.")
            print(f"Use 'mano-cua stop' to stop it first.")
            return 1

        resp.raise_for_status()
        data = resp.json()

        session_id = data["session_id"]
        print(f"Session created: {session_id}")
        
    except Exception as e:
        print(f"Failed to create session: {e}")
        return 1

    try:
        view_model = TaskViewModel(overlay_enabled=not headless)
    except Exception as e:
        _close_session_quietly(base_url, session_id)
        print(f"Failed to initialize task runtime: {e}")
        return 1

    # Start minimized if requested
    if minimize and view_model.view and view_model.view._ui_initialized:
        view_model.view.root.after(200, view_model.view._toggle_minimize)

    # Initialize task with existing session_id
    if not view_model.init_task(task, base_url, expected_result=expected_result, session_id=session_id):
        _close_session_quietly(base_url, session_id)
        print("Failed to initialize visualization overlay.")
        return 1

    # Run task
    success = view_model.run_task()
    # Clean up resources
    view_model.close()
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description="VLA Desktop Automation Client")
    parser.add_argument("command", choices=["run", "stop"], help="Command to execute")
    parser.add_argument("task", nargs="?", help="Task description (required for 'run')")
    parser.add_argument("--expected-result", help="Expected result description for validation", default=None)
    parser.add_argument("--minimize", help="Start with minimized UI panel", action="store_true", default=False)
    parser.add_argument("--server-url", help="Override orchestrator base URL", default=None)
    parser.add_argument(
        "--headless",
        "--no-overlay",
        dest="headless",
        help="Run without the overlay UI panel",
        action="store_true",
        default=False,
    )

    args = parser.parse_args()

    if args.command == "stop":
        return stop_session(server_url=args.server_url)

    if args.command == "run":
        if not args.task:
            print("Error: task is required for 'run' command")
            return 1
        return run_task(
            args.task,
            args.expected_result,
            minimize=args.minimize,
            server_url=args.server_url,
            headless=args.headless,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())

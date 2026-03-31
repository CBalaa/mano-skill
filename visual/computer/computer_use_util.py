import base64
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Dict, Optional

import mss
import mss.tools
from pynput import mouse

from visual.config.visual_config import AUTOMATION_CONFIG

def screenshot_to_bytes():
    """Capture primary screen and return PNG bytes"""
    with mss.mss() as sct:
        screenshot = sct.grab(sct.monitors[1])
        return mss.tools.to_png(screenshot.rgb, screenshot.size)

def b64_png(png_bytes: bytes) -> str:
    """Encode PNG bytes to base64 string"""
    return base64.b64encode(png_bytes).decode("utf-8")

def make_tool_result(tool_use_id: str, ok: bool, message: str,
                     include_screenshot: bool, screenshot_bytes: Optional[bytes],
                     meta: Optional[Dict[str, Any]]=None):
    """Build tool result"""
    tr: Dict[str, Any] = {
        "tool_use_id": tool_use_id,
        "status": "success" if ok else "error",
        "output": message,
        "error": None if ok else message,
        "include_screenshot": bool(include_screenshot),
        "meta": meta or {},
    }
    if include_screenshot and screenshot_bytes:
        tr["screenshot_b64"] = b64_png(screenshot_bytes)
    return tr

def focus_on_primary_screen():
    """Focus mouse on primary screen center"""
    with mss.mss() as sct:
        primary = sct.monitors[1]
        mouse_controller = mouse.Controller()
        mouse_controller.position = (
            primary["left"] + primary["width"] // 2,
            primary["top"] + primary["height"] // 2
        )

def _find_chrome() -> str:
    """Find Chrome/Chromium binary on the system."""
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif system == "Windows":
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    else:  # Linux
        candidates = []

    for path in candidates:
        if os.path.isfile(path):
            return path

    # Fall back to PATH lookup
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise RuntimeError(
        "Chrome/Chromium not found. Install Google Chrome or set its path in PATH."
    )


def capture_web_screenshot(url: str) -> bytes:
    """Capture a viewport screenshot of a web page using headless Chrome.
    Returns PNG bytes."""
    chrome = _find_chrome()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()

    try:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--screenshot={tmp.name}", "--window-size=1920,1080", url],
            capture_output=True,
            timeout=30,
        )
        with open(tmp.name, "rb") as f:
            data = f.read()
        if not data:
            raise RuntimeError("Chrome screenshot returned empty output")
        return data
    finally:
        os.unlink(tmp.name)


def get_or_create_device_id():
    """Get or create device ID"""
    device_file = os.path.expanduser(AUTOMATION_CONFIG["DEVICE_FILE"])
    if os.path.exists(device_file):
        with open(device_file, "r") as f:
            return f.read().strip()

    device_id = str(uuid.uuid4())
    with open(device_file, "w") as f:
        f.write(device_id)
    return device_id
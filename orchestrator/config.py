import os
from dataclasses import dataclass


def _parse_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _parse_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    planner_mode: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    openai_reasoning_effort: str
    openai_timeout: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("MANO_ORCHESTRATOR_HOST", "127.0.0.1"),
            port=_parse_int("MANO_ORCHESTRATOR_PORT", 8000),
            planner_mode=os.getenv("MANO_PLANNER_MODE", "auto").strip().lower() or "auto",
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=(
                os.getenv("MANO_OPENAI_BASE_URL")
                or os.getenv("OPENAI_BASE_URL")
                or ""
            ).strip(),
            openai_model=os.getenv("MANO_OPENAI_MODEL", "gpt-5.4").strip() or "gpt-5.4",
            openai_reasoning_effort=os.getenv("MANO_OPENAI_REASONING_EFFORT", "medium").strip() or "medium",
            openai_timeout=_parse_float("MANO_OPENAI_TIMEOUT", 90.0),
        )

    @property
    def openai_enabled(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings.from_env()

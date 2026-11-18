"""Provider engine routing."""

from typing import Any

from ..config import state
from .gemini import run_gemini_turn
from .universal_loop import run_openai_or_anthropic_turn


def run_agent_turn(user_content: Any, allow_tools: bool = True) -> str:
    """Unified entry point routing to the native Gemini SDK or OpenAI/Anthropic engines."""
    if state.provider == "gemini":
        return run_gemini_turn(user_content, allow_tools=allow_tools)
    return run_openai_or_anthropic_turn(user_content, allow_tools=allow_tools)


__all__ = ["run_agent_turn"]

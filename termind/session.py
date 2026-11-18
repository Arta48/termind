"""System prompt assembly, project rules loading, and conversation lifecycle."""

import os
from typing import Optional

from .config import OS_NAME, get_gemini_client, state
from .mcp_client import mcp_manager


def load_project_rules(announce: bool = False) -> str:
    """Loads custom project guidelines from .agentrules, .cursorrules, or PROJECT.md."""
    for rf in (".agentrules", ".cursorrules", "PROJECT.md"):
        path = os.path.join(state.workspace_dir, rf)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(4000)
                if announce:
                    print(f"\033[92m[Loaded project guidelines from {rf}]\033[0m")
                return f"\nProject Guidelines ({rf}):\n{content}\n"
            except Exception:
                pass
    return ""


def build_system_prompt(announce_rules: bool = False) -> str:
    """Constructs the system instruction including OS context, rules, and compacted summary."""
    prompt = (
        f"You are an autonomous engineering AI assistant operating in the {OS_NAME} environment.\n"
        f"Your working directory (sandbox): {state.workspace_dir}\n"
        "Available tools: list_directory, read_file, edit_file_replace, write_file, "
        "delete_file, search_code, execute_shell_command, fetch_webpage_content, view_image.\n"
        "RULES:\n"
        "1. When modifying existing code, ALWAYS use 'edit_file_replace' to change only the "
        "required lines rather than rewriting the full file.\n"
        "2. Always inspect the project first ('list_directory', 'search_code', 'read_file') "
        "before making changes.\n"
        "3. Use 'view_image' whenever you need to visually inspect local images, screenshots, or diagrams.\n"
        f"4. Be concise, accurate, and respect the current OS ({OS_NAME}).\n"
    )

    mcp_summary = mcp_manager.get_prompt_summary()
    if mcp_summary:
        prompt += mcp_summary

    prompt += load_project_rules(announce=announce_rules)
    if state.session_summary:
        prompt += f"\nPrevious Session Summary:\n{state.session_summary}\n"
    return prompt


def refresh_active_session() -> None:
    """Refreshes the system prompt without discarding active conversation memory."""
    if state.universal_history:
        state.universal_history[0]["content"] = build_system_prompt()
    if state.provider == "gemini":
        try:
            get_gemini_client()
        except Exception:
            pass


def reset_conversation(seed_context: Optional[str] = None, announce_rules: bool = False) -> None:
    """Clears conversation memory and optionally seeds a compacted session summary."""
    state.session_summary = seed_context
    state.gemini_history = []
    state.universal_history = [
        {"role": "system", "content": build_system_prompt(announce_rules=announce_rules)}
    ]

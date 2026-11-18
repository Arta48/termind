"""Interactive terminal REPL, readline history, slash commands, and workspace startup."""

import atexit
import base64
import mimetypes
import os
import re
import sys
from typing import Any, List, Optional, Tuple

from .config import (
    CONFIG_DIR,
    CONFIG_FILE,
    HISTORY_FILE,
    IMAGE_EXTENSIONS,
    MAX_ATTACH_TEXT_CHARS,
    MAX_IMAGE_BYTES,
    OS_NAME,
    PROVIDERS,
    get_provider_api_key,
    get_provider_base_url,
    load_user_config,
    mask_secret,
    save_user_config,
    state,
)
from .engines import run_agent_turn
from .engines.streaming import HAS_RICH
from .mcp_client import mcp_manager
from .security import check_safe_path, is_binary_file
from .session import refresh_active_session, reset_conversation


readline = None
try:
    import readline  # type: ignore[no-redef]
except ImportError:
    try:
        import pyreadline3 as readline  # type: ignore[no-redef]
    except ImportError:
        pass


def setup_readline_history() -> None:
    """Loads persistent terminal command history and registers save-on-exit hook."""
    if not readline:
        return

    legacy_history = os.path.expanduser("~/.universal_agent_history")
    read_target = HISTORY_FILE if os.path.exists(HISTORY_FILE) else legacy_history
    if os.path.exists(read_target):
        try:
            readline.read_history_file(read_target)
        except Exception:
            pass

    def _save_history() -> None:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            readline.set_history_length(2000)
            readline.write_history_file(HISTORY_FILE)
        except Exception:
            pass

    atexit.register(_save_history)


def init_workspace_from_args() -> None:
    """Resolves the target workspace directory from CLI arguments and enters it."""
    if len(sys.argv) > 1:
        if not os.path.isdir(sys.argv[1]):
            print(f"\033[91m[Error: '{sys.argv[1]}' is not a valid directory]\033[0m")
            sys.exit(1)
        state.workspace_dir = os.path.realpath(sys.argv[1])
        os.chdir(state.workspace_dir)
    else:
        state.workspace_dir = os.path.realpath(os.getcwd())


def split_command(text: str) -> Tuple[str, str]:
    """Splits a slash-command from its argument string on exact whitespace boundaries."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


def unescape_shell_path(raw_path: str) -> str:
    """Unescapes common drag-and-drop shell path escapes (spaces, parentheses, quotes)."""
    cleaned = raw_path.strip().strip("'\"")
    return re.sub(r"\\([ ()&'\"])", r"\1", cleaned)


def parse_file_command(text: str) -> Tuple[str, str]:
    """Parses /file arguments supporting quoted paths, spaces, and drag-and-drop escapes."""
    stripped = text.strip()
    match = re.match(r'^/file\s+(["\'])(.+?)\1(?:\s+(.*))?$', stripped)
    if match:
        return match.group(2).strip(), (match.group(3) or "").strip() or "Analyze this file."

    cmd_body = stripped[len("/file"):].strip()
    words = cmd_body.split()
    for i in range(len(words), 0, -1):
        candidate = unescape_shell_path(" ".join(words[:i]))
        candidate_abs = os.path.join(state.workspace_dir, candidate)
        if os.path.exists(candidate_abs):
            return candidate, " ".join(words[i:]).strip() or "Analyze this file."

    parts = cmd_body.split(maxsplit=1)
    return (unescape_shell_path(parts[0]) if parts else ""), (parts[1].strip() if len(parts) > 1 else "Analyze this file.")


def get_safe_markdown_fence(content: str) -> str:
    """Returns a backtick fence strictly longer than any backtick run inside content."""
    longest = 2
    for match in re.finditer(r"`+", content):
        longest = max(longest, len(match.group(0)))
    return "`" * (longest + 1)


def print_banner(show_header: bool = True) -> None:
    """Prints the startup banner and interactive command reference."""
    if show_header:
        print("=" * 65)
        print("🤖 Termind Activated.")
        print(f"💻 OS: {OS_NAME} | 📁 Workspace: {state.workspace_dir}")
        print(f"🔌 Provider: \033[1m{state.provider}\033[0m | 🧠 Model: \033[1m{state.model}\033[0m")
        print(f"💡 Thinking Mode: \033[1m{state.thinking_level}\033[0m")
        print("🛡️ Sandbox: Enabled | History: Persistent")
        if not HAS_RICH:
            print("ℹ️  Tip: Install 'rich' (pip install rich) for live Markdown rendering.")
    print("📋 Available Commands:")
    print("  • /config           — show active configuration and masked API keys")
    print("  • /provider <name>  — switch provider: gemini, deepseek, openai, claude, groq, ollama, openrouter")
    print("  • /model <name>     — switch model (keeps conversation memory)")
    print("  • /thinking <level> — set reasoning level: off, low, medium, high")
    print("  • /key <api_key>    — set API key for active provider (or 'reset')")
    print("  • /base_url <url>   — set custom endpoint URL for active provider (or 'reset')")
    print("  • /yolo             — toggle auto-approval mode [y/N]")
    print("  • /file <path>      — attach image (Vision) or text/code file")
    print("  • /mcp [cmd]        — manage MCP plugins: status, connect <name>, disconnect <name>, reload")
    print("  • /reset            — clear conversation memory")
    print("  • /compact          — summarize and compact chat history")
    print("  • /help             — show this command list")
    print("  • \"\"\"               — enter multiline paste mode")
    print("  • exit              — quit")
    print("=" * 65)


def handle_file_attachment(user_input: str) -> Optional[Any]:
    """Reads and validates an attached text or image file for /file commands."""
    file_path, prompt_text = parse_file_command(user_input)
    is_safe, resolved = check_safe_path(file_path)
    if not is_safe:
        print(resolved)
        return None
    if not file_path or not os.path.exists(resolved):
        print(f"Error: file '{file_path}' not found.")
        return None
    if os.path.isdir(resolved):
        print(f"Error: '{file_path}' is a directory, not a file.")
        return None

    ext = os.path.splitext(resolved)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        try:
            file_size = os.path.getsize(resolved)
        except OSError as e:
            print(f"Error inspecting '{file_path}': {e}")
            return None
        if file_size > MAX_IMAGE_BYTES:
            print(f"Error: image '{file_path}' exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit.")
            return None

        mime_type = mimetypes.guess_type(resolved)[0] or "image/png"
        with open(resolved, "rb") as f:
            img_bytes = f.read()

        print(f"[Attached image: {file_path}]")
        if state.provider == "gemini":
            from google.genai import types

            return [types.Part.from_bytes(data=img_bytes, mime_type=mime_type), prompt_text]

        b64_img = base64.b64encode(img_bytes).decode("utf-8")
        return [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
        ]

    if is_binary_file(resolved):
        print(f"Error: '{file_path}' is a binary file and cannot be attached as text.")
        return None

    try:
        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            file_body = f.read(MAX_ATTACH_TEXT_CHARS + 1)
    except Exception as e:
        print(f"Error reading file '{file_path}': {e}")
        return None

    truncated_note = ""
    if len(file_body) > MAX_ATTACH_TEXT_CHARS:
        file_body = file_body[:MAX_ATTACH_TEXT_CHARS]
        truncated_note = f"\n... [truncated to {MAX_ATTACH_TEXT_CHARS} characters]"

    fence = get_safe_markdown_fence(file_body)
    print(f"[Attached text file: {file_path} ({len(file_body)} chars)]")
    return f"{prompt_text}\n\n[Attached File: {file_path}]\n{fence}\n{file_body}{truncated_note}\n{fence}"


def read_multiline_input(initial_text: str) -> Optional[str]:
    """Reads triple-quoted multiline paste input; returns None if cancelled via Ctrl+C."""
    quote_char = initial_text[:3]
    rest = initial_text[3:]
    if rest.endswith(quote_char) and len(rest) >= 3:
        return rest[:-3]

    lines: List[str] = []
    if rest:
        lines.append(rest)
    print(f"[Multiline mode: paste content and enter {quote_char} on a new line to send (Ctrl+C to cancel)]")

    while True:
        try:
            line = input("... ")
        except (KeyboardInterrupt, EOFError):
            print("\n[Multiline input cancelled]")
            return None
        if line.strip() == quote_char:
            break
        lines.append(line)

    return "\n".join(lines).strip()


def main() -> None:
    """Initializes workspace, history, user config, MCP plugins, and runs the interactive REPL."""
    init_workspace_from_args()
    setup_readline_history()
    load_user_config()
    atexit.register(mcp_manager.shutdown)
    mcp_manager.connect_on_startup()
    reset_conversation(announce_rules=True)
    print_banner(show_header=True)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting agent.")
            break

        if not user_input:
            continue

        cmd, arg = split_command(user_input)

        if cmd in ("exit", "quit"):
            print("Session terminated.")
            break

        if cmd == "/help":
            print_banner(show_header=False)
            continue

        if cmd == "/yolo":
            state.yolo_mode = not state.yolo_mode
            status = "ENABLED (auto-approvals) ⚡" if state.yolo_mode else "DISABLED (requires [y/N]) 🛡️"
            print(f"\n[YOLO Mode: {status}]")
            continue

        if cmd == "/reset":
            reset_conversation()
            print("\n[Conversation context has been completely reset]")
            continue

        if cmd == "/compact":
            print("\n[Compacting conversation context...]")
            summary_text = run_agent_turn(
                "Generate a concise, structured bulleted summary of our conversation so far: "
                "current goal, modified files, findings, and pending tasks.",
                allow_tools=False,
            )
            if summary_text.strip():
                reset_conversation(seed_context=summary_text.strip())
                print("\033[92m[Context successfully compacted with preserved summary!]\033[0m")
            else:
                print("\033[93m[Compacting aborted: empty summary returned]\033[0m")
            continue

        if cmd == "/config":
            active_key = get_provider_api_key(state.provider)
            active_url = get_provider_base_url(state.provider) or "(native SDK)"
            print(f"\nActive Configuration ({CONFIG_FILE}):")
            print(f"  • Provider: {state.provider}")
            print(f"  • Model:    {state.model}")
            print(f"  • Thinking: {state.thinking_level}")
            print(f"  • Base URL: {active_url}")
            print(f"  • API Key:  {mask_secret(active_key)}")
            continue

        if cmd == "/provider":
            if arg:
                p_name = arg.lower()
                if p_name in PROVIDERS:
                    state.provider = p_name
                    state.model = (
                        state.saved_providers.get(p_name, {}).get("model")
                        or PROVIDERS[p_name]["default_model"]
                    )
                    reset_conversation()
                    save_user_config()
                    print(f"\n[Switched provider to '{state.provider}' (Model: {state.model})]")
                else:
                    print(f"\n[Unknown provider. Available: {', '.join(PROVIDERS.keys())}]")
            else:
                print(f"\nCurrent provider: {state.provider} | Available: {', '.join(PROVIDERS.keys())}")
            continue

        if cmd == "/model":
            if arg:
                state.model = arg
                refresh_active_session()
                save_user_config()
                print(f"\n[Model updated to '{state.model}' (conversation memory preserved)]")
            else:
                print(f"\nCurrent model: {state.model}")
            continue

        if cmd == "/thinking":
            if arg:
                lvl = arg.lower()
                if lvl in ("off", "low", "medium", "high"):
                    state.thinking_level = lvl
                    refresh_active_session()
                    save_user_config()
                    print(f"\n[Thinking mode set to: '{lvl}' (conversation memory preserved)]")
                else:
                    print("\n[Usage: /thinking <off|low|medium|high>]")
            else:
                print(f"\nCurrent thinking mode: {state.thinking_level}")
            continue

        if cmd == "/key":
            if arg:
                state.provider_overrides.setdefault(state.provider, {})
                if arg.lower() == "reset":
                    state.provider_overrides[state.provider].pop("api_key", None)
                    state.saved_providers.get(state.provider, {}).pop("api_key", None)
                    print(f"\n[API Key override cleared for '{state.provider}']")
                else:
                    state.provider_overrides[state.provider]["api_key"] = arg
                    print(f"\n[API Key updated and saved for '{state.provider}']")
                refresh_active_session()
                save_user_config()
            else:
                print("\n[Usage: /key <api_key> or /key reset]")
            continue

        if cmd == "/base_url":
            if arg:
                state.provider_overrides.setdefault(state.provider, {})
                if arg.lower() == "reset":
                    state.provider_overrides[state.provider].pop("base_url", None)
                    state.saved_providers.get(state.provider, {}).pop("base_url", None)
                    print(f"\n[Custom Base URL cleared for '{state.provider}']")
                else:
                    state.provider_overrides[state.provider]["base_url"] = arg
                    print(f"\n[Custom Base URL for '{state.provider}' saved: {arg}]")
                save_user_config()
            else:
                print(f"\nCurrent Base URL for '{state.provider}': {get_provider_base_url(state.provider)}")
            continue

        if cmd == "/file":
            payload = handle_file_attachment(user_input)
            if payload is not None:
                run_agent_turn(payload)
            continue

        if cmd == "/mcp":
            sub_parts = arg.split(maxsplit=1)
            sub_cmd = sub_parts[0].lower() if sub_parts else "status"
            sub_arg = sub_parts[1].strip() if len(sub_parts) > 1 else ""

            if sub_cmd == "status":
                print(f"\n{mcp_manager.status_report()}")
            elif sub_cmd == "reload":
                mcp_manager.disconnect_all()
                mcp_manager.connect_on_startup()
                refresh_active_session()
            elif sub_cmd == "connect":
                if not sub_arg:
                    print("\n[Usage: /mcp connect <server_name|all>]")
                elif sub_arg.lower() == "all":
                    mcp_manager.discover_configs()
                    for srv in list(mcp_manager._configs.keys()):
                        _, msg = mcp_manager.connect(srv)
                        print(f"[MCP] {msg}")
                    refresh_active_session()
                else:
                    mcp_manager.discover_configs()
                    _, msg = mcp_manager.connect(sub_arg)
                    print(f"\n[MCP] {msg}")
                    refresh_active_session()
            elif sub_cmd == "disconnect":
                if not sub_arg:
                    print("\n[Usage: /mcp disconnect <server_name|all>]")
                elif sub_arg.lower() == "all":
                    for srv in list(mcp_manager._sessions.keys()):
                        print(f"[MCP] {mcp_manager.disconnect(srv)}")
                    refresh_active_session()
                else:
                    print(f"\n[MCP] {mcp_manager.disconnect(sub_arg)}")
                    refresh_active_session()
            else:
                print("\n[Usage: /mcp [status|connect <name|all>|disconnect <name|all>|reload]]")
            continue

        if user_input.startswith('"""') or user_input.startswith("'''"):
            multiline_text = read_multiline_input(user_input)
            if not multiline_text:
                continue
            user_input = multiline_text

        run_agent_turn(user_input)

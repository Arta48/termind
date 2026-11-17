#!/usr/bin/env python3
"""
Universal Autonomous Engineering AI Agent for Terminal (Linux / Windows / macOS).
Supports Google Gemini (Native GenAI SDK), OpenAI, DeepSeek, Anthropic (Claude),
Groq, Ollama, and OpenRouter.

Key Capabilities:
- Hybrid native engines preserving true model reasoning (CoT, signatures, thinking blocks)
- Cross-platform workspace sandboxing with symlink resolution & HITL approval
- Non-blocking shell execution with process-tree timeout protection
- Surgical file editing (uniqueness & line-ending safe), recursive code search, SSRF-safe web fetch
- Autonomous visual file inspection (view_image) and /file attachments
- Live Markdown rendering with Rich and persistent readline command history
"""

import asyncio
import atexit
import base64
from contextlib import AsyncExitStack
import copy
from dataclasses import dataclass, field
import html as html_lib
import ipaddress
import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.parse
import urllib.request
import uuid


# =====================================================================
# 1. PLATFORM DETECTION & TERMINAL INPUT (READLINE / RICH)
# =====================================================================

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    OS_NAME = "Windows (PowerShell)"
elif IS_MACOS:
    OS_NAME = "macOS (Zsh)"
else:
    OS_NAME = "Linux (Bash)"

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
    history_file = os.path.expanduser("~/.universal_agent_history")
    if os.path.exists(history_file):
        try:
            readline.read_history_file(history_file)
        except Exception:
            pass

    def _save_history() -> None:
        try:
            readline.set_history_length(2000)
            readline.write_history_file(history_file)
        except Exception:
            pass

    atexit.register(_save_history)


try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown

    console: Optional[Console] = Console()
    HAS_RICH = True
except ImportError:
    console = None
    HAS_RICH = False


# =====================================================================
# 2. CONFIGURATION, CONSTANTS & SESSION STATE
# =====================================================================

WORKSPACE_DIR = os.path.realpath(os.getcwd())

MAX_TOOL_STEPS = 40
MAX_READ_LINES = 1000
MAX_READ_LINE_CHARS = 2000
MAX_READ_OUTPUT_CHARS = 20_000
MAX_LIST_DIR_ITEMS = 500
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_SHELL_OUTPUT_CHARS = 12_000
MAX_WEB_FETCH_BYTES = 512 * 1024
MAX_WEB_TEXT_CHARS = 6000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_ATTACH_TEXT_CHARS = 200_000
MAX_MCP_TOOL_NAME_LEN = 64
MCP_CALL_TIMEOUT_SEC = 120.0

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
BINARY_EXTENSIONS = (
    ".class", ".jar", ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".o", ".a", ".zip", ".tar", ".gz", ".7z", ".rar", ".pdf", ".ico", ".woff",
    ".woff2", ".ttf", ".mp3", ".mp4", ".wav", ".db", ".sqlite", ".sqlite3",
)

DANGEROUS_COMMANDS = [
    # Linux / Unix destructive patterns
    r"\brm\s+.*(-[a-zA-Z]*r|-[a-zA-Z]*f|--recursive|--force).*\s+(?:(?:/\*?|~(?:/\S*)?|\*|\$HOME(?:/\S*)?)(?=$|\s|;|&&|\|)|/(?:bin|boot|dev|etc|home|lib|lib64|opt|proc|root|run|sbin|srv|sys|usr|var)\b)",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r">{1,2}\s*/dev/(sd|nvme|hd|vd)",
    # Windows destructive patterns
    r"\bformat(\.com)?\s+[a-zA-Z]:",
    r"\b(Format-Volume|Clear-Disk|Stop-Computer|Restart-Computer)\b",
    r"\b(del|erase|rd|rmdir)\s+.*[a-zA-Z]:\\",
    r"\bRemove-Item\b.*-Recurse.*[a-zA-Z]:\\",
]

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "engine": "google-genai",
        "api_key_env": "GEMINI_API_KEY",
        "default_model": "gemini-3.8-flash",
    },
    "openai": {
        "engine": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "deepseek": {
        "engine": "openai",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-flash",
    },
    "claude": {
        "engine": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
    },
    "groq": {
        "engine": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
    },
    "ollama": {
        "engine": "openai",
        "base_url": "http://localhost:11434/v1",
        "api_key_env": None,
        "default_model": "qwen2.5-coder:latest",
    },
    "openrouter": {
        "engine": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4.6",
    },
}


@dataclass
class AgentState:
    """Encapsulates active provider, credentials, mode flags, and conversation history."""

    provider: str = "gemini"
    model: str = PROVIDERS["gemini"]["default_model"]
    thinking_level: str = "high"
    yolo_mode: bool = False
    session_summary: Optional[str] = None
    provider_overrides: Dict[str, Dict[str, str]] = field(default_factory=dict)
    gemini_client: Optional[Any] = None
    gemini_client_key: Optional[str] = None
    gemini_history: List[Any] = field(default_factory=list)
    universal_history: List[Dict[str, Any]] = field(default_factory=list)


state = AgentState()


def get_provider_api_key(provider_name: str) -> str:
    """Returns per-provider override key if set, otherwise reads its environment variable."""
    override_key = state.provider_overrides.get(provider_name, {}).get("api_key")
    if override_key:
        return override_key
    env_var = PROVIDERS.get(provider_name, {}).get("api_key_env")
    return os.environ.get(env_var, "") if env_var else ""


def get_provider_base_url(provider_name: str) -> Optional[str]:
    """Returns per-provider override URL if set, otherwise returns default endpoint URL."""
    override_url = state.provider_overrides.get(provider_name, {}).get("base_url")
    if override_url:
        return override_url
    return PROVIDERS.get(provider_name, {}).get("base_url")


def get_gemini_client() -> Any:
    """Returns a cached Google GenAI client, rebuilding automatically if the key changed."""
    from google import genai

    api_key = get_provider_api_key("gemini")
    if state.gemini_client is None or state.gemini_client_key != api_key:
        state.gemini_client = genai.Client(api_key=api_key or None)
        state.gemini_client_key = api_key
    return state.gemini_client


def load_project_rules(announce: bool = False) -> str:
    """Loads custom project guidelines from .agentrules, .cursorrules, or PROJECT.md."""
    for rf in (".agentrules", ".cursorrules", "PROJECT.md"):
        path = os.path.join(WORKSPACE_DIR, rf)
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
        f"Your working directory (sandbox): {WORKSPACE_DIR}\n"
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


# =====================================================================
# 3. SECURITY SANDBOX & HUMAN-IN-THE-LOOP (HITL)
# =====================================================================

def check_safe_path(file_path: str) -> Tuple[bool, str]:
    """Validates that the resolved path (including symlinks) remains inside WORKSPACE_DIR."""
    if not file_path or not file_path.strip():
        return False, "Access blocked: empty path provided."
    resolved_target = os.path.realpath(os.path.join(WORKSPACE_DIR, file_path))
    try:
        if os.path.commonpath([WORKSPACE_DIR, resolved_target]) != WORKSPACE_DIR:
            return False, f"Access blocked: path '{file_path}' resolves outside the workspace sandbox."
    except ValueError:
        return False, f"Access blocked: path '{file_path}' is located on a different drive."
    return True, resolved_target


def is_within_workspace(abs_path: str) -> bool:
    """Returns True if abs_path resolves inside WORKSPACE_DIR after symlink resolution."""
    resolved = os.path.realpath(abs_path)
    try:
        return os.path.commonpath([WORKSPACE_DIR, resolved]) == WORKSPACE_DIR
    except ValueError:
        return False


def ask_user_permission(action: str, details: str = "") -> bool:
    """Prompts the user in the terminal to authorize mutating or shell operations."""
    if state.yolo_mode:
        print(f"\n\033[93m[⚡ YOLO]: Auto-approved: {action}\033[0m")
        return True

    print("\n\033[93m[⚠️ PERMISSION REQUIRED]\033[0m")
    print(f"Action: \033[1m{action}\033[0m")
    if details:
        preview = details if len(details) <= 1500 else details[:1500] + "\n... (preview truncated)"
        print(f"\033[90m{preview}\033[0m")
    try:
        ans = input("Authorize execution? [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        return False


def is_binary_file(abs_path: str) -> bool:
    """Checks extension and null bytes to detect binary files safely."""
    ext = os.path.splitext(abs_path)[1].lower()
    if ext in IMAGE_EXTENSIONS or ext in BINARY_EXTENSIONS:
        return True
    try:
        with open(abs_path, "rb") as f:
            chunk = f.read(1024)
        return b"\x00" in chunk
    except Exception:
        return False


def is_public_url(url: str) -> Tuple[bool, str]:
    """Blocks non-HTTP(S) schemes and resolves hostnames to reject loopback/private/link-local IPs."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, "Error: URL must begin with http:// or https://"
    hostname = parsed.hostname
    if not hostname:
        return False, "Error: URL does not contain a valid hostname."
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return False, "Blocked by security policy: loopback URLs are not allowed."

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"Error resolving hostname '{hostname}': {e}"

    for info in addr_infos:
        ip_str = info[4][0].split("%")[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if getattr(ip_obj, "ipv4_mapped", None):
                ip_obj = ip_obj.ipv4_mapped
        except ValueError:
            return False, f"Blocked by security policy: invalid resolved IP '{ip_str}'."
        if (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            return False, f"Blocked by security policy: private/internal IP address ({ip_str}) is forbidden."
    return True, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validates every HTTP redirect destination against SSRF rules before following."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        is_allowed, err_msg = is_public_url(newurl)
        if not is_allowed:
            raise ValueError(err_msg)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# =====================================================================
# 4. AGENT TOOLS IMPLEMENTATION
# =====================================================================

def list_directory(relative_path: str = ".") -> str:
    """List files and folders in the specified directory."""
    is_safe, path = check_safe_path(relative_path)
    if not is_safe:
        return path
    if not os.path.exists(path):
        return f"Directory '{relative_path}' does not exist."
    if not os.path.isdir(path):
        return f"Error: '{relative_path}' is a file, not a directory."
    try:
        items = sorted(os.listdir(path))
        truncated = len(items) > MAX_LIST_DIR_ITEMS
        selected = items[:MAX_LIST_DIR_ITEMS]
        output = []
        for item in selected:
            item_path = os.path.join(path, item)
            tag = "[DIR]" if os.path.isdir(item_path) else "[FILE]"
            output.append(f"{tag} {item}")
        if truncated:
            output.append(f"... (truncated at {MAX_LIST_DIR_ITEMS} of {len(items)} items)")
        return "\n".join(output) if output else "(directory is empty)"
    except Exception as e:
        return f"Error reading directory: {e}"


def read_file(file_path: str, start_line: int = 1, max_lines: int = 250) -> str:
    """Read text file content lazily with line pagination and character limits."""
    is_safe, abs_path = check_safe_path(file_path)
    if not is_safe:
        return abs_path
    if not os.path.exists(abs_path):
        return f"File '{file_path}' not found."
    if os.path.isdir(abs_path):
        return f"Error: '{file_path}' is a directory. Use 'list_directory' instead."

    ext = os.path.splitext(abs_path)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return f"Notice: '{file_path}' is an image file. Use the 'view_image' tool to visually inspect it."
    if is_binary_file(abs_path):
        return f"Notice: '{file_path}' is a binary file and cannot be read as plain text."

    try:
        start_num = max(1, int(start_line))
        limit_num = max(1, min(int(max_lines), MAX_READ_LINES))
    except (TypeError, ValueError):
        return "Error: 'start_line' and 'max_lines' must be integers."

    start_idx = start_num - 1
    end_idx = start_idx + limit_num

    try:
        selected: List[str] = []
        total_chars = 0
        has_more = False
        char_capped = False

        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                if idx < start_idx:
                    continue
                if idx >= end_idx:
                    has_more = True
                    break

                if len(line) > MAX_READ_LINE_CHARS:
                    line = line[:MAX_READ_LINE_CHARS] + "... [line truncated]\n"

                formatted = f"{idx + 1:4d} | {line}"
                if total_chars + len(formatted) > MAX_READ_OUTPUT_CHARS:
                    char_capped = True
                    has_more = True
                    break

                selected.append(formatted)
                total_chars += len(formatted)

        if not selected:
            if start_num > 1:
                return f"(no content at or after line {start_num})"
            return "(file is empty)"

        result = "".join(selected)
        if char_capped:
            result += f"\n... (output truncated at {MAX_READ_OUTPUT_CHARS} chars; use smaller max_lines)"
        elif has_more:
            result += f"\n... (more lines available; pass start_line={end_idx + 1} to continue)"
        return result
    except Exception as e:
        return f"Error reading file: {e}"


def edit_file_replace(file_path: str, target_snippet: str, replacement_snippet: str) -> str:
    """Surgically replace a unique code or text snippet while preserving original line endings."""
    is_safe, abs_path = check_safe_path(file_path)
    if not is_safe:
        return abs_path
    if not os.path.exists(abs_path):
        return f"File '{file_path}' not found."
    if os.path.isdir(abs_path):
        return f"Error: '{file_path}' is a directory, not a file."
    if is_binary_file(abs_path):
        return f"Error: '{file_path}' is a binary file and cannot be edited as text."
    if not target_snippet:
        return "Error: 'target_snippet' cannot be empty."

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace", newline="") as f:
            content = f.read()

        target = target_snippet
        replacement = replacement_snippet
        occurrences = content.count(target)

        # Adapt LF snippets cleanly when the target file on disk uses CRLF line endings
        if occurrences == 0 and "\r\n" in content and "\r\n" not in target:
            crlf_target = target.replace("\n", "\r\n")
            if content.count(crlf_target) > 0:
                target = crlf_target
                replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
                occurrences = content.count(target)

        if occurrences == 0:
            return f"Error: target snippet not found in file '{file_path}'. Check exact whitespace and indentation."
        if occurrences > 1:
            return (
                f"Error: target snippet appears {occurrences} times in '{file_path}'. "
                "Provide more surrounding context lines to uniquely identify the target block."
            )

        diff = f"\033[91m--- Remove:\n{target}\033[0m\n\033[92m+++ Add:\n{replacement}\033[0m"
        if not ask_user_permission(f"Edit file: {file_path}", diff):
            return "Cancelled by user: edit rejected."

        new_content = content.replace(target, replacement, 1)
        with open(abs_path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        return f"File '{file_path}' successfully updated."
    except Exception as e:
        return f"Error editing file: {e}"


def write_file(file_path: str, content: str) -> str:
    """Create a new file or completely overwrite an existing one."""
    is_safe, abs_path = check_safe_path(file_path)
    if not is_safe:
        return abs_path
    if os.path.isdir(abs_path):
        return f"Error: '{file_path}' is an existing directory and cannot be overwritten as a file."

    preview = content[:300] + ("..." if len(content) > 300 else "")
    if not ask_user_permission(f"Full write to file: {file_path}", f"Content:\n{preview}"):
        return "Cancelled by user: write rejected."

    try:
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return f"File '{file_path}' successfully saved ({len(content)} characters)."
    except Exception as e:
        return f"Error writing file: {e}"


def delete_file(file_path: str) -> str:
    """Delete a specified file from the workspace."""
    is_safe, abs_path = check_safe_path(file_path)
    if not is_safe:
        return abs_path
    if not os.path.exists(abs_path):
        return f"File '{file_path}' not found."
    if os.path.isdir(abs_path):
        return f"Error: '{file_path}' is a directory. Use shell commands if removal is needed."

    if not ask_user_permission(f"DELETE FILE: {file_path}"):
        return "Cancelled by user: deletion rejected."

    try:
        os.remove(abs_path)
        return f"File '{file_path}' deleted."
    except Exception as e:
        return f"Error deleting file: {e}"


def search_code(query: str, path: str = ".", extension: str = "") -> str:
    """Recursively search for text across non-binary workspace files (symlink-safe grep)."""
    if not query:
        return "Error: search query cannot be empty."
    is_safe, abs_search_path = check_safe_path(path)
    if not is_safe:
        return abs_search_path
    if not os.path.exists(abs_search_path):
        return f"Path '{path}' not found."

    ignored_dirs = {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
        "__pycache__", ".mypy_cache", ".pytest_cache", "build", "dist", "target", ".idea",
    }
    matches: List[str] = []
    max_matches = 40
    query_lower = query.lower()

    for root, dirs, files in os.walk(abs_search_path):
        dirs[:] = [
            d for d in sorted(dirs)
            if d not in ignored_dirs and is_within_workspace(os.path.join(root, d))
        ]
        for file in sorted(files):
            if extension and not file.endswith(extension):
                continue
            full_path = os.path.join(root, file)
            if not is_within_workspace(full_path):
                continue
            try:
                if os.path.getsize(full_path) > MAX_SEARCH_FILE_BYTES:
                    continue
            except OSError:
                continue
            if is_binary_file(full_path):
                continue

            rel_path = os.path.relpath(full_path, WORKSPACE_DIR)
            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_num, line in enumerate(f, 1):
                        if query_lower in line.lower():
                            matches.append(f"{rel_path}:{line_num}: {line.strip()[:140]}")
                            if len(matches) >= max_matches:
                                return "\n".join(matches) + f"\n... (truncated at {max_matches} matches)"
            except Exception:
                continue

    return "\n".join(matches) if matches else f"No matches found for '{query}'."


def execute_shell_command(command: str) -> str:
    """Execute a shell command with live streaming output and process-tree timeout enforcement."""
    for pattern in DANGEROUS_COMMANDS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"BLOCKED BY SECURITY: command '{command}' matches a dangerous pattern."

    if not ask_user_permission(f"Execute command: $ {command}"):
        return "Cancelled by user: execution rejected."

    timeout_sec = 180.0
    timed_out = threading.Event()
    process: Optional[subprocess.Popen[str]] = None

    def _kill_process_tree() -> None:
        if process is None or process.poll() is not None:
            return
        timed_out.set()
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    timer = threading.Timer(timeout_sec, _kill_process_tree)

    try:
        print(f"\n\033[94m[Running command ({OS_NAME})]: $ {command}\033[0m")
        popen_kwargs: Dict[str, Any] = {
            "cwd": WORKSPACE_DIR,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }

        if IS_WINDOWS:
            ps_utf8_prefix = (
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            )
            cmd_target: Union[str, List[str]] = [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-Command", ps_utf8_prefix + command,
            ]
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            cmd_target = command
            popen_kwargs["shell"] = True
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(cmd_target, **popen_kwargs)
        timer.start()

        output_lines: List[str] = []
        if process.stdout:
            for line in process.stdout:
                print(line, end="", flush=True)
                output_lines.append(line)
        process.wait()
        timer.cancel()

        if timed_out.is_set():
            return f"Error: command timed out after {int(timeout_sec)}s and was terminated."

        captured = "".join(output_lines).strip()
        if len(captured) > MAX_SHELL_OUTPUT_CHARS:
            captured = (
                captured[:4000]
                + "\n\n... [output truncated] ...\n\n"
                + captured[-(MAX_SHELL_OUTPUT_CHARS - 4000):]
            )

        if process.returncode != 0:
            return f"(Command exited with code {process.returncode})\n{captured or '(no output)'}"
        return captured or "(command completed successfully)"
    except Exception as e:
        timer.cancel()
        return f"Error executing command: {e}"


def fetch_webpage_content(url: str) -> str:
    """Download and sanitize public web page text with SSRF, redirect, and size protections."""
    is_allowed, err_msg = is_public_url(url)
    if not is_allowed:
        return err_msg

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"Mozilla/5.0 (AI Agent {OS_NAME})"},
        )
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(req, timeout=15) as response:
            raw_bytes = response.read(MAX_WEB_FETCH_BYTES + 1)
            was_truncated = len(raw_bytes) > MAX_WEB_FETCH_BYTES
            raw_bytes = raw_bytes[:MAX_WEB_FETCH_BYTES]

            charset = response.headers.get_content_charset() or "utf-8"
            html_text = raw_bytes.decode(charset, errors="replace")

        cleaned = re.sub(r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html_lib.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if len(cleaned) > MAX_WEB_TEXT_CHARS:
            return cleaned[:MAX_WEB_TEXT_CHARS] + "\n...(truncated)"
        if was_truncated:
            return cleaned + "\n...(source response truncated)"
        return cleaned or "(no readable text extracted)"
    except Exception as e:
        return f"Error fetching webpage: {e}"


def view_image(file_path: str, query: str = "Describe what is depicted in this image in detail.") -> str:
    """Visually inspect and analyze an image file located in the workspace using the active provider."""
    is_safe, abs_path = check_safe_path(file_path)
    if not is_safe:
        return abs_path
    if not os.path.exists(abs_path):
        return f"File '{file_path}' not found."
    if os.path.isdir(abs_path):
        return f"Error: '{file_path}' is a directory, not an image file."

    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return f"Error: '{file_path}' is not a supported image format."

    try:
        file_size = os.path.getsize(abs_path)
    except OSError as e:
        return f"Error inspecting file '{file_path}': {e}"

    if file_size > MAX_IMAGE_BYTES:
        return f"Error: image '{file_path}' exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB size limit."

    print(f"\n\033[94m[Visual Inspection: {file_path}]\033[0m")

    mime_type = mimetypes.guess_type(abs_path)[0] or "image/png"
    with open(abs_path, "rb") as f:
        img_bytes = f.read()

    try:
        if state.provider == "gemini":
            from google.genai import types

            v_client = get_gemini_client()
            part = types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
            resp = v_client.models.generate_content(
                model=state.model,
                contents=[part, f"Visually analyze this image and answer the query: {query}"],
            )
            return resp.text or "(No visual description produced)"

        p_info = PROVIDERS.get(state.provider, PROVIDERS["openai"])
        api_key = get_provider_api_key(state.provider)
        base_url = get_provider_base_url(state.provider)
        b64_img = base64.b64encode(img_bytes).decode("utf-8")

        if p_info["engine"] == "anthropic":
            import anthropic

            c_client = anthropic.Anthropic(api_key=api_key or "placeholder")
            resp = c_client.messages.create(
                model=state.model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64_img}},
                        {"type": "text", "text": f"Visually analyze this image and answer: {query}"},
                    ],
                }],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

        import openai

        o_client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key or ("ollama" if state.provider == "ollama" else "placeholder"),
        )
        msg_payload = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Visually analyze this image and answer: {query}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
            ],
        }]
        try:
            resp = o_client.chat.completions.create(
                model=state.model,
                messages=msg_payload,
                max_tokens=1024,
            )
        except Exception as err:
            if "max_tokens" in str(err) and "max_completion_tokens" in str(err):
                resp = o_client.chat.completions.create(
                    model=state.model,
                    messages=msg_payload,
                    max_completion_tokens=1024,
                )
            else:
                raise
        return resp.choices[0].message.content or "(No visual description produced)"

    except Exception as e:
        return (
            f"Error analyzing image with active provider '{state.provider}' ({state.model}): {e}. "
            "If this model does not support vision inputs, switch to a multimodal model or provider "
            "(e.g. '/provider gemini' or '/model ...')."
        )


NATIVE_TOOLS_LIST = [
    list_directory,
    read_file,
    edit_file_replace,
    write_file,
    delete_file,
    search_code,
    execute_shell_command,
    fetch_webpage_content,
    view_image,
]

TOOLS_MAP = {fn.__name__: fn for fn in NATIVE_TOOLS_LIST}

TOOLS_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories in the specified path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "description": "Relative directory path", "default": "."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read text file contents with line pagination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"},
                    "start_line": {"type": "integer", "description": "Start line number (1-based)", "default": 1},
                    "max_lines": {"type": "integer", "description": "Max lines to read", "default": 250},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file_replace",
            "description": "Replace a unique snippet in a file with a replacement snippet. Best for surgical code edits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"},
                    "target_snippet": {"type": "string", "description": "Exact unique text snippet to replace"},
                    "replacement_snippet": {"type": "string", "description": "New replacement text"},
                },
                "required": ["file_path", "target_snippet", "replacement_snippet"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or completely overwrite an existing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative file path"}
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Recursively search for a text query across non-binary project files (built-in grep).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword or symbol"},
                    "path": {"type": "string", "description": "Subfolder to search in", "default": "."},
                    "extension": {"type": "string", "description": "File extension filter (e.g. '.py')", "default": ""},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell_command",
            "description": "Execute a shell command with live real-time output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command string to execute"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage_content",
            "description": "Download text from a public web page or documentation link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public URL starting with http:// or https://"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "Inspect and visually analyze an image file (PNG, JPG, WEBP, GIF) in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Relative path to the image file"},
                    "query": {
                        "type": "string",
                        "description": "Specific visual question about the image",
                        "default": "Describe what is depicted in this image in detail.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
]

class MCPManager:
    """Manages optional MCP servers (stdio & Streamable HTTP) over a background asyncio loop."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._configs: Dict[str, Tuple[Dict[str, Any], str]] = {}  # name -> (cfg, source_label)
        self._stacks: Dict[str, AsyncExitStack] = {}
        self._sessions: Dict[str, Any] = {}
        self._tools: Dict[str, Tuple[str, str, str, Dict[str, Any]]] = {}  # exposed -> (server, orig_name, desc, schema)

    @staticmethod
    def is_available() -> bool:
        try:
            import mcp  # noqa: F401
            return True
        except ImportError:
            return False

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or not self._loop.is_running():
                ready = threading.Event()

                def _runner() -> None:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    self._loop = loop
                    ready.set()
                    loop.run_forever()

                self._thread = threading.Thread(target=_runner, daemon=True, name="termind-mcp")
                self._thread.start()
                ready.wait()
            return self._loop  # type: ignore[return-value]

    def _run_coro(self, coro: Any, timeout: float = 30.0) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def discover_configs(self) -> Tuple[List[str], List[str]]:
        """Loads ~/.termind/mcp.json (trusted) and returns any workspace-local server names separately."""
        self._configs.clear()
        global_names: List[str] = []
        local_names: List[str] = []

        candidates = [
            (os.path.expanduser("~/.termind/mcp.json"), "global", False),
            (os.path.join(WORKSPACE_DIR, ".mcp.json"), "workspace", True),
        ]
        for cfg_path, label, is_local in candidates:
            if not os.path.isfile(cfg_path):
                continue
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                servers = data.get("mcpServers") or data.get("servers") or {}
                if not isinstance(servers, dict):
                    continue
                for name, srv_cfg in servers.items():
                    if isinstance(srv_cfg, dict) and not srv_cfg.get("disabled", False):
                        self._configs[name] = (srv_cfg, label)
                        (local_names if is_local else global_names).append(name)
            except Exception as e:
                print(f"\033[93m[Warning: Failed to parse {cfg_path}: {e}]\033[0m")

        return global_names, local_names

    @staticmethod
    def _sanitize_tool_name(server: str, tool_name: str) -> str:
        s_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", server)
        t_clean = re.sub(r"[^a-zA-Z0-9_-]", "_", tool_name)
        candidate = f"mcp__{s_clean}__{t_clean}"
        return candidate[:MAX_MCP_TOOL_NAME_LEN]

    async def _async_connect(self, name: str, cfg: Dict[str, Any]) -> List[str]:
        import httpx2
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client

        if name in self._stacks:
            await self._async_disconnect(name)

        stack = AsyncExitStack()
        try:
            if "url" in cfg:
                url = str(cfg["url"])
                headers = cfg.get("headers") or {}
                http_client = None
                if headers:
                    http_client = await stack.enter_async_context(
                        httpx2.AsyncClient(
                            headers={str(k): str(v) for k, v in headers.items()},
                            timeout=httpx2.Timeout(30, read=300),
                        )
                    )
                read_s, write_s, *_ = await stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
            elif "command" in cfg:
                raw_env = cfg.get("env")
                env_dict = {str(k): str(v) for k, v in raw_env.items()} if isinstance(raw_env, dict) else None
                raw_cwd = cfg.get("cwd")
                cwd_path = os.path.realpath(os.path.join(WORKSPACE_DIR, raw_cwd)) if raw_cwd else WORKSPACE_DIR
                params = StdioServerParameters(
                    command=str(cfg["command"]),
                    args=[str(a) for a in (cfg.get("args") or [])],
                    env=env_dict,
                    cwd=cwd_path,
                )
                read_s, write_s = await stack.enter_async_context(stdio_client(params))
            else:
                raise ValueError("Server config must contain either 'command' or 'url'.")

            session = await stack.enter_async_context(ClientSession(read_s, write_s))
            await session.initialize()
            tools_resp = await session.list_tools()

            self._stacks[name] = stack
            self._sessions[name] = session

            added: List[str] = []
            for tool in tools_resp.tools:
                exposed = self._sanitize_tool_name(name, tool.name)
                suffix_idx = 2
                while exposed in self._tools and self._tools[exposed][:2] != (name, tool.name):
                    suffix = f"_{suffix_idx}"
                    exposed = exposed[: MAX_MCP_TOOL_NAME_LEN - len(suffix)] + suffix
                    suffix_idx += 1

                schema = dict(tool.input_schema or {"type": "object", "properties": {}})
                schema.setdefault("type", "object")
                desc = f"[MCP:{name}] {tool.description or tool.name}"
                self._tools[exposed] = (name, tool.name, desc, schema)
                added.append(exposed)
            return added
        except Exception:
            await stack.aclose()
            raise

    async def _async_disconnect(self, name: str) -> None:
        self._sessions.pop(name, None)
        for k in [k for k, v in self._tools.items() if v[0] == name]:
            self._tools.pop(k, None)
        stack = self._stacks.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    def connect(self, name: str) -> Tuple[bool, str]:
        if not self.is_available():
            return False, "MCP SDK not installed. Run: pip install mcp (requires Python 3.11+)."
        if name not in self._configs:
            return False, f"Unknown MCP server '{name}'."
        cfg, _ = self._configs[name]
        try:
            added = self._run_coro(self._async_connect(name, cfg), timeout=45.0)
            return True, f"Connected '{name}' ({len(added)} tools)"
        except Exception as e:
            return False, f"Failed to connect '{name}': {e}"

    def disconnect(self, name: str) -> str:
        if name not in self._sessions and name not in self._stacks:
            return f"Server '{name}' is not connected."
        try:
            self._run_coro(self._async_disconnect(name), timeout=15.0)
        except Exception:
            pass
        return f"Disconnected '{name}'."

    def connect_on_startup(self) -> None:
        global_names, local_names = self.discover_configs()
        if not (global_names or local_names) or not self.is_available():
            return

        for name in global_names:
            ok, msg = self.connect(name)
            color = "92" if ok else "93"
            print(f"\033[{color}m[MCP] {msg}\033[0m")

        if local_names:
            print(f"\n\033[93m[⚠️ WORKSPACE MCP CONFIG DETECTED]: {', '.join(local_names)}\033[0m")
            for name in local_names:
                cfg, _ = self._configs[name]
                preview = cfg.get("url") or f"{cfg.get('command')} {' '.join(cfg.get('args') or [])}"
                if ask_user_permission(f"Start workspace MCP server '{name}'", str(preview)):
                    ok, msg = self.connect(name)
                    color = "92" if ok else "93"
                    print(f"\033[{color}m[MCP] {msg}\033[0m")

    def has_tool(self, exposed_name: str) -> bool:
        return exposed_name in self._tools

    def call_tool(self, exposed_name: str, fn_args: Dict[str, Any]) -> str:
        server_name, orig_name, _, _ = self._tools[exposed_name]
        session = self._sessions.get(server_name)
        if session is None:
            return f"Error: MCP server '{server_name}' is disconnected."

        details = json.dumps(fn_args, ensure_ascii=False, indent=2) if fn_args else "(no arguments)"
        if not ask_user_permission(f"MCP Tool [{server_name}]: {orig_name}", details):
            return "Cancelled by user: MCP tool execution rejected."

        async def _invoke() -> Any:
            return await session.call_tool(orig_name, arguments=fn_args)

        try:
            print(f"\n\033[94m[MCP Call: {server_name}.{orig_name}]\033[0m")
            res = self._run_coro(_invoke(), timeout=MCP_CALL_TIMEOUT_SEC)
            parts: List[str] = []
            for item in res.content or []:
                if getattr(item, "text", None) is not None:
                    parts.append(str(item.text))
                elif getattr(item, "type", "") == "image":
                    parts.append(f"[Image returned: {getattr(item, 'mime_type', None) or 'image/*'}]")
                else:
                    parts.append(str(item))
            text_out = "\n".join(parts).strip() or "(no output)"
            if len(text_out) > MAX_SHELL_OUTPUT_CHARS:
                text_out = text_out[:4000] + "\n... [MCP output truncated] ...\n" + text_out[-(MAX_SHELL_OUTPUT_CHARS - 4000):]
            if res.is_error:
                return f"MCP Tool Error:\n{text_out}"
            return text_out
        except Exception as e:
            return f"MCP tool call failed: {e}"

    def get_openai_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": exposed,
                    "description": desc,
                    "parameters": schema,
                },
            }
            for exposed, (_, _, desc, schema) in self._tools.items()
        ]

    def get_gemini_declarations(self, types_mod: Any) -> List[Any]:
        decls = []
        for exposed, (_, _, desc, schema) in self._tools.items():
            decls.append(
                types_mod.FunctionDeclaration(
                    name=exposed,
                    description=desc,
                    parameters_json_schema=schema,
                )
            )
        return decls

    def get_prompt_summary(self) -> str:
        if not self._sessions:
            return ""
        lines = ["\nConnected External MCP Tools:"]
        for exposed, (srv, orig, desc, _) in self._tools.items():
            lines.append(f"- {exposed} (server '{srv}', tool '{orig}'): {desc[:120]}")
        return "\n".join(lines) + "\n"

    def status_report(self) -> str:
        if not self.is_available():
            return "MCP Python SDK is not installed. Install with: pip install mcp (requires Python 3.11+)."
        if not self._configs:
            return "No MCP servers configured. Add servers to ~/.termind/mcp.json or ./.mcp.json."
        lines = ["MCP Servers:"]
        for name, (cfg, label) in self._configs.items():
            connected = name in self._sessions
            t_count = sum(1 for v in self._tools.values() if v[0] == name)
            target = cfg.get("url") or cfg.get("command", "?")
            mark = f"CONNECTED ({t_count} tools)" if connected else "disconnected"
            lines.append(f"  • {name} [{label}] ({target}) — {mark}")
        return "\n".join(lines)

    def disconnect_all(self) -> None:
        """Closes all active MCP sessions while keeping the background event loop alive."""
        if self._loop is None or not self._loop.is_running():
            return

        async def _close_all() -> None:
            for name in list(self._stacks.keys()):
                await self._async_disconnect(name)

        try:
            self._run_coro(_close_all(), timeout=10.0)
        except Exception:
            pass

    def shutdown(self) -> None:
        self.disconnect_all()
        if self._loop is not None and self._loop.is_running():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self._lock:
            self._loop = None
            self._thread = None


mcp_manager = MCPManager()


def execute_tool_call(fn_name: str, fn_args: Any) -> str:
    """Safely invokes a built-in or connected MCP tool with validated arguments."""
    if not isinstance(fn_args, dict):
        return f"Error: tool arguments for '{fn_name}' must be a JSON object."
    if mcp_manager.has_tool(fn_name):
        return mcp_manager.call_tool(fn_name, fn_args)
    tool_fn = TOOLS_MAP.get(fn_name)
    if not tool_fn:
        return f"Error: unknown tool '{fn_name}'"
    try:
        return str(tool_fn(**fn_args))
    except Exception as e:
        return f"Tool execution error: {e}"


# =====================================================================
# 5. STREAMING HELPERS, FORMAT CONVERTERS & EXECUTION ENGINES
# =====================================================================

class ThinkTagStreamParser:
    """Splits inline <think>...</think> blocks cleanly across arbitrary stream chunk boundaries."""

    def __init__(self) -> None:
        self.in_think = False
        self.buffer = ""

    def feed(self, text: str) -> List[Tuple[bool, str]]:
        """Returns a list of (is_thought, segment_text) tuples."""
        self.buffer += text
        events: List[Tuple[bool, str]] = []

        while self.buffer:
            tag = "</think>" if self.in_think else "<think>"
            idx = self.buffer.find(tag)
            if idx != -1:
                if idx > 0:
                    events.append((self.in_think, self.buffer[:idx]))
                self.in_think = not self.in_think
                self.buffer = self.buffer[idx + len(tag):]
                continue

            # Hold back any trailing prefix of the target tag to prevent split-tag leakage
            hold = 0
            max_prefix = min(len(tag) - 1, len(self.buffer))
            for length in range(max_prefix, 0, -1):
                if tag.startswith(self.buffer[-length:]):
                    hold = length
                    break

            emit_len = len(self.buffer) - hold
            if emit_len > 0:
                events.append((self.in_think, self.buffer[:emit_len]))
                self.buffer = self.buffer[emit_len:]
            break

        return events

    def flush(self) -> List[Tuple[bool, str]]:
        if not self.buffer:
            return []
        rem = self.buffer
        self.buffer = ""
        return [(self.in_think, rem)]


class StreamRenderer:
    """Coordinates terminal output for reasoning streams and live Markdown responses."""

    def __init__(self) -> None:
        self.in_thought = False
        self.full_text = ""
        self.live: Optional[Any] = None

    def emit_thought(self, text: str) -> None:
        if not text:
            return
        self.stop_live()
        if not self.in_thought:
            print("\n\033[90m[Agent Reasoning]:\033[0m")
            self.in_thought = True
        print(f"\033[90m{text}\033[0m", end="", flush=True)

    def emit_text(self, text: str) -> None:
        if not text:
            return
        if self.in_thought:
            print()
            self.in_thought = False
        if self.live is None:
            if HAS_RICH and console is not None:
                console.print("\n[bold cyan]🤖 [Agent]:[/bold cyan]")
                self.live = Live(console=console, refresh_per_second=12)
                self.live.start()
            else:
                print("\n\033[1m[Agent]:\033[0m\n", end="", flush=True)
        self.full_text += text
        if self.live is not None:
            self.live.update(Markdown(self.full_text))
        else:
            print(text, end="", flush=True)

    def stop_live(self) -> None:
        if self.live is not None:
            try:
                self.live.stop()
            finally:
                self.live = None

    def finish_step(self) -> None:
        if self.in_thought:
            print()
            self.in_thought = False
        self.stop_live()
        if self.full_text and not HAS_RICH:
            print()


def print_token_usage(prompt_tokens: int, output_tokens: int, total_tokens: int) -> None:
    """Prints token counts in dimmed style."""
    meta_str = f"Tokens: prompt: {prompt_tokens} | output: {output_tokens} | total: {total_tokens}"
    if HAS_RICH and console is not None:
        console.print(f"[dim]{meta_str}[/dim]")
    else:
        print(f"\033[90m{meta_str}\033[0m")


def is_retryable_error(err: Exception) -> bool:
    """Detects transient rate-limit and service-unavailable API errors."""
    status = getattr(err, "status_code", None)
    if status in (429, 503, 529):
        return True
    err_str = str(err).lower()
    return any(k in err_str for k in ("503", "429", "529", "unavailable", "resource_exhausted", "overloaded"))


def is_bad_request_error(err: Exception) -> bool:
    """Detects HTTP 400/422 validation errors triggered by unsupported optional parameters."""
    status = getattr(err, "status_code", None)
    if status in (400, 422):
        return True
    err_str = str(err)
    return "400" in err_str or "422" in err_str


# --- Gemini Native Engine Helpers ---

def build_gemini_thinking_config(types_mod: Any, model_name: str, level: str) -> Optional[Any]:
    """Selects model-compatible ThinkingConfig (thinking_budget for 2.5, thinking_level for 3+/4)."""
    m = model_name.lower()
    if "gemini-1." in m or "gemini-2.0" in m:
        return None
    if "gemini-2.5" in m:
        if level == "off":
            budget = 128 if "pro" in m else 0
        elif level == "low":
            budget = 2048
        elif level == "medium":
            budget = 6144
        else:
            budget = -1
        return types_mod.ThinkingConfig(
            thinking_budget=budget,
            include_thoughts=(level != "off"),
        )

    # Models like gemini-3.8-flash and gemini-4-* reject "minimal" and only accept low/medium/high
    supports_minimal = any(
        tag in m for tag in ("gemini-3-flash", "gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "robotics")
    )
    effective_level = ("minimal" if supports_minimal else "low") if level == "off" else level

    return types_mod.ThinkingConfig(
        thinking_level=effective_level,
        include_thoughts=(level != "off"),
    )


def build_gemini_user_content(types_mod: Any, user_content: Any) -> Any:
    """Converts string or multimodal list payloads into a google-genai Content object."""
    if isinstance(user_content, str):
        parts = [types_mod.Part(text=user_content)]
    elif isinstance(user_content, list):
        parts = [
            types_mod.Part(text=item) if isinstance(item, str) else item
            for item in user_content
        ]
    else:
        parts = [user_content]
    return types_mod.Content(role="user", parts=parts)


def merge_gemini_stream_part(collected_parts: List[Any], part: Any) -> None:
    """Appends or merges a streamed Gemini Part while strictly preserving all thought signatures."""
    part_copy = part.model_copy() if hasattr(part, "model_copy") else copy.copy(part)

    has_structured_payload = any(
        getattr(part_copy, attr, None) is not None
        for attr in ("function_call", "function_response", "inline_data", "file_data", "executable_code", "code_execution_result")
    )
    if has_structured_payload:
        collected_parts.append(part_copy)
        return

    incoming_text = getattr(part_copy, "text", None) or ""
    incoming_sig = getattr(part_copy, "thought_signature", None)
    incoming_thought = bool(getattr(part_copy, "thought", False))

    if not incoming_text and incoming_sig is not None:
        if collected_parts and getattr(collected_parts[-1], "thought_signature", None) is None:
            collected_parts[-1].thought_signature = incoming_sig
        else:
            collected_parts.append(part_copy)
        return

    if not incoming_text and incoming_sig is None:
        return

    if collected_parts:
        prev = collected_parts[-1]
        prev_is_structured = any(
            getattr(prev, attr, None) is not None
            for attr in ("function_call", "function_response", "inline_data", "file_data", "executable_code", "code_execution_result")
        )
        prev_sig = getattr(prev, "thought_signature", None)
        can_merge = (
            not prev_is_structured
            and bool(getattr(prev, "thought", False)) == incoming_thought
            and (prev_sig is None or incoming_sig is None)
        )
        if can_merge:
            prev.text = (getattr(prev, "text", None) or "") + incoming_text
            if incoming_sig is not None:
                prev.thought_signature = incoming_sig
            return

    collected_parts.append(part_copy)


def run_gemini_turn(user_content: Any, allow_tools: bool = True) -> str:
    """Executes a multi-step Gemini turn with manual tool handling and preserved thought signatures."""
    from google.genai import types

    try:
        client = get_gemini_client()
    except Exception as e:
        print(f"\n\033[91m[Error initializing Gemini client: {e}]\033[0m")
        return ""

    turn_start_idx = len(state.gemini_history)
    state.gemini_history.append(build_gemini_user_content(types, user_content))

    max_retries = 6
    base_delay = 2
    final_answer = ""

    try:
        for _step in range(MAX_TOOL_STEPS):
            thinking_cfg = build_gemini_thinking_config(types, state.model, state.thinking_level)
            mcp_decls = mcp_manager.get_gemini_declarations(types)
            gemini_tools: List[Any]
            if mcp_decls:
                native_decls = [
                    types.FunctionDeclaration.from_callable(
                        client=client,
                        callable=fn,
                        use_json_schema=True,
                    )
                    for fn in NATIVE_TOOLS_LIST
                ]
                gemini_tools = [types.Tool(function_declarations=native_decls + mcp_decls)]
            else:
                gemini_tools = NATIVE_TOOLS_LIST

            cfg_kwargs: Dict[str, Any] = {
                "system_instruction": build_system_prompt(),
                "tools": gemini_tools,
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            }
            if thinking_cfg is not None:
                cfg_kwargs["thinking_config"] = thinking_cfg
            if not allow_tools:
                cfg_kwargs["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="NONE")
                )

            collected_parts: List[Any] = []
            last_usage = None
            step_succeeded = False

            for attempt in range(max_retries):
                renderer = StreamRenderer()
                collected_parts = []
                last_usage = None

                try:
                    cfg = types.GenerateContentConfig(**cfg_kwargs)
                    response_stream = client.models.generate_content_stream(
                        model=state.model,
                        contents=state.gemini_history,
                        config=cfg,
                    )

                    for chunk in response_stream:
                        if getattr(chunk, "usage_metadata", None):
                            last_usage = chunk.usage_metadata
                        if not chunk.candidates or not chunk.candidates[0].content:
                            continue

                        for part in chunk.candidates[0].content.parts or []:
                            merge_gemini_stream_part(collected_parts, part)

                            if getattr(part, "function_call", None):
                                renderer.stop_live()
                                continue
                            if not getattr(part, "text", None):
                                continue

                            if getattr(part, "thought", False):
                                renderer.emit_thought(part.text)
                            else:
                                renderer.emit_text(part.text)

                    renderer.finish_step()
                    step_succeeded = True
                    break

                except KeyboardInterrupt:
                    renderer.finish_step()
                    raise
                except Exception as e:
                    renderer.finish_step()
                    if "thinking_config" in cfg_kwargs and is_bad_request_error(e):
                        cfg_kwargs.pop("thinking_config", None)
                        continue
                    if is_retryable_error(e) and attempt < max_retries - 1:
                        sleep_s = min(60, base_delay * (2 ** attempt))
                        print(f"\n\033[93m[Server busy. Retrying in {sleep_s}s... Attempt {attempt + 1}/{max_retries}]\033[0m")
                        time.sleep(sleep_s)
                        continue
                    print(f"\n\033[91m[Execution Error]: {e}\033[0m")
                    del state.gemini_history[turn_start_idx:]
                    return ""

            if not step_succeeded:
                del state.gemini_history[turn_start_idx:]
                return ""

            if last_usage:
                print_token_usage(
                    getattr(last_usage, "prompt_token_count", 0) or 0,
                    getattr(last_usage, "candidates_token_count", 0) or 0,
                    getattr(last_usage, "total_token_count", 0) or 0,
                )

            if collected_parts:
                state.gemini_history.append(types.Content(role="model", parts=collected_parts))

            function_calls = [
                p.function_call for p in collected_parts if getattr(p, "function_call", None) is not None
            ]

            if not function_calls or not allow_tools:
                final_answer = "".join(
                    p.text for p in collected_parts
                    if getattr(p, "text", None) and not getattr(p, "thought", False)
                )
                break

            response_parts: List[Any] = []
            for fc in function_calls:
                fn_args = dict(fc.args) if getattr(fc, "args", None) else {}
                tool_result = execute_tool_call(fc.name, fn_args)
                fr_part = types.Part.from_function_response(
                    name=fc.name,
                    response={"result": tool_result or "(no output)"},
                )
                if getattr(fc, "id", None) and getattr(fr_part, "function_response", None) is not None:
                    fr_part.function_response.id = fc.id
                response_parts.append(fr_part)

            state.gemini_history.append(types.Content(role="user", parts=response_parts))
        else:
            print(f"\n\033[93m[Stopped after reaching {MAX_TOOL_STEPS} tool steps]\033[0m")

    except KeyboardInterrupt:
        print("\n\033[93m[Generation interrupted by user]\033[0m")
        del state.gemini_history[turn_start_idx:]
        return ""

    return final_answer


# --- OpenAI-Compatible & Anthropic Engines ---

def prepare_openai_messages(
    history: List[Dict[str, Any]],
    provider_name: str,
    include_reasoning: bool = True,
) -> List[Dict[str, Any]]:
    """Builds OpenAI-format messages, preserving reasoning_content for DeepSeek/reasoning endpoints."""
    prepared: List[Dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        entry: Dict[str, Any] = {"role": role}

        if role == "assistant":
            entry["content"] = msg.get("content", "")
            if msg.get("tool_calls"):
                entry["tool_calls"] = msg["tool_calls"]
            needs_reasoning = (
                include_reasoning
                and (
                    (provider_name == "deepseek" and state.thinking_level != "off")
                    or msg.get("_has_native_reasoning", False)
                )
            )
            if needs_reasoning:
                entry["reasoning_content"] = msg.get("reasoning_content", "")
        elif role == "tool":
            entry["tool_call_id"] = msg["tool_call_id"]
            if msg.get("name"):
                entry["name"] = msg["name"]
            entry["content"] = msg.get("content", "")
        else:
            entry["content"] = msg.get("content", "")

        prepared.append(entry)
    return prepared


def convert_history_for_anthropic(history: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """Converts canonical conversation history into strict Anthropic Messages format."""
    system_msg = ""
    anthropic_msgs: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def _append_user_blocks(blocks: List[Dict[str, Any]]) -> None:
        if not blocks:
            return
        if anthropic_msgs and anthropic_msgs[-1]["role"] == "user":
            prev = anthropic_msgs[-1]["content"]
            if isinstance(prev, str):
                prev_blocks = [{"type": "text", "text": prev}] if prev else []
            else:
                prev_blocks = list(prev)
            anthropic_msgs[-1]["content"] = prev_blocks + blocks
        else:
            anthropic_msgs.append({"role": "user", "content": blocks})

    def _flush_tool_results() -> None:
        if pending_tool_results:
            _append_user_blocks(list(pending_tool_results))
            pending_tool_results.clear()

    for msg in history:
        role = msg.get("role")
        if role == "system":
            system_msg = msg.get("content", "")
            continue

        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg.get("content", "") or "(no output)",
            })
            continue

        _flush_tool_results()

        if role == "user":
            content = msg.get("content")
            if isinstance(content, list):
                converted_blocks: List[Dict[str, Any]] = []
                for item in content:
                    if item.get("type") == "text":
                        converted_blocks.append({"type": "text", "text": item["text"]})
                    elif item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        match = re.match(r"^data:([^;]+);base64,(.+)$", url, re.DOTALL)
                        if match:
                            converted_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": match.group(1),
                                    "data": match.group(2),
                                },
                            })
                _append_user_blocks(converted_blocks)
            else:
                _append_user_blocks([{"type": "text", "text": content or ""}])

        elif role == "assistant":
            # Replay untouched Anthropic blocks (thinking + signature, redacted_thinking, text, tool_use)
            if msg.get("anthropic_blocks"):
                anthropic_msgs.append({
                    "role": "assistant",
                    "content": copy.deepcopy(msg["anthropic_blocks"]),
                })
                continue

            blocks: List[Dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls", []):
                try:
                    tool_input = json.loads(tc["function"]["arguments"]) if tc["function"].get("arguments") else {}
                    if not isinstance(tool_input, dict):
                        tool_input = {}
                except Exception:
                    tool_input = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": tool_input,
                })
            if blocks:
                anthropic_msgs.append({"role": "assistant", "content": blocks})

    _flush_tool_results()
    return system_msg, anthropic_msgs


def serialize_anthropic_content(blocks: List[Any]) -> List[Dict[str, Any]]:
    """Serializes Anthropic SDK response blocks while preserving thinking signatures verbatim."""
    serialized: List[Dict[str, Any]] = []
    for block in blocks:
        b_type = getattr(block, "type", "")
        if b_type == "thinking":
            serialized.append({
                "type": "thinking",
                "thinking": getattr(block, "thinking", ""),
                "signature": getattr(block, "signature", ""),
            })
        elif b_type == "redacted_thinking":
            serialized.append({
                "type": "redacted_thinking",
                "data": getattr(block, "data", ""),
            })
        elif b_type == "text":
            text_val = getattr(block, "text", "")
            if text_val:
                serialized.append({"type": "text", "text": text_val})
        elif b_type == "tool_use":
            serialized.append({
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}) or {},
            })
    return serialized


def run_openai_or_anthropic_turn(user_content: Any, allow_tools: bool = True) -> str:
    """Executes a multi-turn tool loop via OpenAI-compatible or Anthropic SDKs."""
    turn_start_idx = len(state.universal_history)
    state.universal_history.append({"role": "user", "content": user_content})

    p_info = PROVIDERS.get(state.provider, PROVIDERS["openai"])
    p_engine = p_info["engine"]
    api_key = get_provider_api_key(state.provider)
    base_url = get_provider_base_url(state.provider)

    max_retries = 5
    base_delay = 2
    final_answer = ""

    try:
        for _step in range(MAX_TOOL_STEPS):
            active_schemas = TOOLS_SCHEMAS + mcp_manager.get_openai_schemas()
            attempt_succeeded = False
            full_text = ""
            reasoning_text = ""
            has_native_reasoning = False
            anthropic_blocks: Optional[List[Dict[str, Any]]] = None
            tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
            usage_stats: Optional[Tuple[int, int, int]] = None

            for attempt in range(max_retries):
                renderer = StreamRenderer()
                tool_calls_buffer = {}
                usage_stats = None
                reasoning_chunks: List[str] = []
                has_native_reasoning = False
                anthropic_blocks = None
                think_parser = ThinkTagStreamParser()

                try:
                    if p_engine == "openai":
                        import openai

                        client = openai.OpenAI(
                            base_url=base_url,
                            api_key=api_key or ("ollama" if state.provider == "ollama" else "placeholder"),
                        )

                        include_reasoning_hist = True
                        call_kwargs: Dict[str, Any] = {
                            "model": state.model,
                            "messages": prepare_openai_messages(
                                state.universal_history, state.provider, include_reasoning=include_reasoning_hist
                            ),
                            "tools": active_schemas,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        }
                        if not allow_tools:
                            call_kwargs["tool_choice"] = "none"
                        if state.provider == "deepseek":
                            call_kwargs["extra_body"] = {
                                "thinking": {"type": "disabled" if state.thinking_level == "off" else "enabled"}
                            }
                        if state.thinking_level != "off":
                            call_kwargs["reasoning_effort"] = state.thinking_level

                        # Progressively strip optional parameters if endpoint returns 400/422
                        stream = None
                        for strip_opt in (None, "reasoning_effort", "extra_body", "stream_options", "tool_choice", "history_reasoning"):
                            if strip_opt == "reasoning_effort":
                                if "reasoning_effort" not in call_kwargs:
                                    continue
                                call_kwargs.pop("reasoning_effort", None)
                            elif strip_opt == "extra_body":
                                if "extra_body" not in call_kwargs:
                                    continue
                                call_kwargs.pop("extra_body", None)
                            elif strip_opt == "stream_options":
                                if "stream_options" not in call_kwargs:
                                    continue
                                call_kwargs.pop("stream_options", None)
                            elif strip_opt == "tool_choice":
                                if "tool_choice" not in call_kwargs:
                                    continue
                                call_kwargs.pop("tool_choice", None)
                            elif strip_opt == "history_reasoning":
                                if not include_reasoning_hist:
                                    continue
                                include_reasoning_hist = False
                                call_kwargs["messages"] = prepare_openai_messages(
                                    state.universal_history, state.provider, include_reasoning=False
                                )

                            try:
                                stream = client.chat.completions.create(**call_kwargs)
                                break
                            except Exception as err:
                                if not is_bad_request_error(err) or strip_opt == "history_reasoning":
                                    raise

                        for chunk in stream:
                            if getattr(chunk, "usage", None):
                                u = chunk.usage
                                usage_stats = (u.prompt_tokens or 0, u.completion_tokens or 0, u.total_tokens or 0)

                            delta = chunk.choices[0].delta if chunk.choices else None
                            if not delta:
                                continue

                            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                            if reasoning:
                                has_native_reasoning = True
                                reasoning_chunks.append(reasoning)
                                renderer.emit_thought(reasoning)

                            if delta.tool_calls:
                                renderer.stop_live()
                                for tc_delta in delta.tool_calls:
                                    idx = tc_delta.index
                                    fn_delta = tc_delta.function
                                    if idx not in tool_calls_buffer:
                                        tool_calls_buffer[idx] = {
                                            "id": tc_delta.id or "",
                                            "name": (fn_delta.name or "") if fn_delta else "",
                                            "arguments": (fn_delta.arguments or "") if fn_delta else "",
                                        }
                                    else:
                                        if tc_delta.id:
                                            tool_calls_buffer[idx]["id"] += tc_delta.id
                                        if fn_delta and fn_delta.name:
                                            tool_calls_buffer[idx]["name"] += fn_delta.name
                                        if fn_delta and fn_delta.arguments:
                                            tool_calls_buffer[idx]["arguments"] += fn_delta.arguments

                            if delta.content:
                                for is_thought, seg in think_parser.feed(delta.content):
                                    if is_thought:
                                        renderer.emit_thought(seg)
                                    else:
                                        renderer.emit_text(seg)

                        for is_thought, seg in think_parser.flush():
                            if is_thought:
                                renderer.emit_thought(seg)
                            else:
                                renderer.emit_text(seg)

                    elif p_engine == "anthropic":
                        import anthropic

                        client = anthropic.Anthropic(api_key=api_key or "placeholder")
                        anthropic_tools = [
                            {
                                "name": t["function"]["name"],
                                "description": t["function"]["description"],
                                "input_schema": t["function"]["parameters"],
                            }
                            for t in active_schemas
                        ]
                        sys_msg, anthropic_msgs = convert_history_for_anthropic(state.universal_history)

                        anthropic_kwargs: Dict[str, Any] = {
                            "model": state.model,
                            "max_tokens": 8192,
                            "system": sys_msg,
                            "messages": anthropic_msgs,
                            "tools": anthropic_tools,
                        }
                        if not allow_tools:
                            anthropic_kwargs["tool_choice"] = {"type": "none"}
                        if state.thinking_level != "off":
                            budget_map = {"low": 2048, "medium": 5120, "high": 10240}
                            budget = budget_map.get(state.thinking_level, 4096)
                            anthropic_kwargs["max_tokens"] = budget + 8192
                            anthropic_kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}

                        def _open_anthropic_stream() -> Any:
                            try:
                                return client.messages.stream(**anthropic_kwargs)
                            except Exception as err:
                                if "thinking" in anthropic_kwargs and is_bad_request_error(err):
                                    anthropic_kwargs["thinking"] = {"type": "adaptive"}
                                    anthropic_kwargs["max_tokens"] = 16384
                                    try:
                                        return client.messages.stream(**anthropic_kwargs)
                                    except Exception as err2:
                                        if is_bad_request_error(err2):
                                            anthropic_kwargs.pop("thinking", None)
                                            anthropic_kwargs["max_tokens"] = 8192
                                            return client.messages.stream(**anthropic_kwargs)
                                        raise
                                raise

                        with _open_anthropic_stream() as stream:
                            for event in stream:
                                if getattr(event, "type", "") == "content_block_delta":
                                    delta_obj = event.delta
                                    d_type = getattr(delta_obj, "type", "")
                                    if d_type == "thinking_delta":
                                        renderer.emit_thought(getattr(delta_obj, "thinking", ""))
                                    elif d_type == "text_delta":
                                        renderer.emit_text(getattr(delta_obj, "text", ""))

                            resp_msg = stream.get_final_message()
                            if getattr(resp_msg, "usage", None):
                                in_t = getattr(resp_msg.usage, "input_tokens", 0) or 0
                                out_t = getattr(resp_msg.usage, "output_tokens", 0) or 0
                                usage_stats = (in_t, out_t, in_t + out_t)

                            anthropic_blocks = serialize_anthropic_content(resp_msg.content)
                            for block in resp_msg.content:
                                if getattr(block, "type", "") == "tool_use":
                                    tool_calls_buffer[len(tool_calls_buffer)] = {
                                        "id": block.id,
                                        "name": block.name,
                                        "arguments": json.dumps(block.input),
                                    }

                    full_text = renderer.full_text
                    reasoning_text = "".join(reasoning_chunks)
                    renderer.finish_step()
                    attempt_succeeded = True
                    break

                except KeyboardInterrupt:
                    renderer.finish_step()
                    raise
                except Exception as e:
                    renderer.finish_step()
                    if is_retryable_error(e) and attempt < max_retries - 1:
                        sleep_s = min(60, base_delay * (2 ** attempt))
                        print(f"\n\033[93m[Server busy. Retrying in {sleep_s}s... Attempt {attempt + 1}/{max_retries}]\033[0m")
                        time.sleep(sleep_s)
                        continue

                    print(f"\n\033[91m[Execution Error]: {e}\033[0m")
                    del state.universal_history[turn_start_idx:]
                    return ""

            if not attempt_succeeded:
                del state.universal_history[turn_start_idx:]
                return ""

            if usage_stats:
                print_token_usage(*usage_stats)

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": full_text,
            }
            if reasoning_text or has_native_reasoning:
                assistant_msg["reasoning_content"] = reasoning_text
                assistant_msg["_has_native_reasoning"] = True
            if anthropic_blocks is not None:
                assistant_msg["anthropic_blocks"] = anthropic_blocks

            if not tool_calls_buffer or not allow_tools:
                if full_text or anthropic_blocks:
                    state.universal_history.append(assistant_msg)
                final_answer = full_text
                break

            assistant_tool_calls = []
            for idx in sorted(tool_calls_buffer.keys()):
                tc_data = tool_calls_buffer[idx]
                assistant_tool_calls.append({
                    "id": tc_data["id"] or f"call_{idx}_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tc_data["name"],
                        "arguments": tc_data["arguments"],
                    },
                })
            assistant_msg["tool_calls"] = assistant_tool_calls
            state.universal_history.append(assistant_msg)

            for tc in assistant_tool_calls:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                try:
                    fn_args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError as e:
                    tool_result = (
                        f"Error: malformed JSON arguments for tool '{fn_name}': {e}. "
                        f"Raw arguments received: {raw_args!r}"
                    )
                else:
                    tool_result = execute_tool_call(fn_name, fn_args)

                state.universal_history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": tool_result or "(no output)",
                })
        else:
            print(f"\n\033[93m[Stopped after reaching {MAX_TOOL_STEPS} tool steps]\033[0m")

    except KeyboardInterrupt:
        print("\n\033[93m[Generation interrupted by user]\033[0m")
        del state.universal_history[turn_start_idx:]
        return ""

    return final_answer


def run_agent_turn(user_content: Any, allow_tools: bool = True) -> str:
    """Unified entry point routing to the native Gemini SDK or OpenAI/Anthropic engines."""
    if state.provider == "gemini":
        return run_gemini_turn(user_content, allow_tools=allow_tools)
    return run_openai_or_anthropic_turn(user_content, allow_tools=allow_tools)


# =====================================================================
# 6. REPL HELPERS & INTERACTIVE LOOP
# =====================================================================

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
        candidate_abs = os.path.join(WORKSPACE_DIR, candidate)
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
        print(f"💻 OS: {OS_NAME} | 📁 Workspace: {WORKSPACE_DIR}")
        print(f"🔌 Provider: \033[1m{state.provider}\033[0m | 🧠 Model: \033[1m{state.model}\033[0m")
        print(f"💡 Thinking Mode: \033[1m{state.thinking_level}\033[0m")
        print("🛡️ Sandbox: Enabled | History: Persistent")
        if not HAS_RICH:
            print("ℹ️  Tip: Install 'rich' (pip install rich) for live Markdown rendering.")
    print("📋 Available Commands:")
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
    """Initializes terminal history, project rules, and runs the interactive agent REPL."""
    setup_readline_history()
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

        if cmd == "/provider":
            if arg:
                p_name = arg.lower()
                if p_name in PROVIDERS:
                    state.provider = p_name
                    state.model = PROVIDERS[p_name]["default_model"]
                    reset_conversation()
                    print(f"\n[Switched provider to '{state.provider}' (Default model: {state.model})]")
                else:
                    print(f"\n[Unknown provider. Available: {', '.join(PROVIDERS.keys())}]")
            else:
                print(f"\nCurrent provider: {state.provider} | Available: {', '.join(PROVIDERS.keys())}")
            continue

        if cmd == "/model":
            if arg:
                state.model = arg
                refresh_active_session()
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
                    print(f"\n[API Key override cleared for '{state.provider}']")
                else:
                    state.provider_overrides[state.provider]["api_key"] = arg
                    print(f"\n[API Key updated for '{state.provider}']")
                refresh_active_session()
            else:
                print("\n[Usage: /key <api_key> or /key reset]")
            continue

        if cmd == "/base_url":
            if arg:
                state.provider_overrides.setdefault(state.provider, {})
                if arg.lower() == "reset":
                    state.provider_overrides[state.provider].pop("base_url", None)
                    print(f"\n[Custom Base URL cleared for '{state.provider}']")
                else:
                    state.provider_overrides[state.provider]["base_url"] = arg
                    print(f"\n[Custom Base URL for '{state.provider}' set to: {arg}]")
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


if __name__ == "__main__":
    main()

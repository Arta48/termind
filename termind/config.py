"""Configuration, platform constants, provider registry, and session state."""

import copy
from dataclasses import dataclass, field
import json
import os
import sys
from typing import Any, Dict, List, Optional


IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    OS_NAME = "Windows (PowerShell)"
elif IS_MACOS:
    OS_NAME = "macOS (Zsh)"
else:
    OS_NAME = "Linux (Bash)"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".termind")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
MCP_GLOBAL_CONFIG = os.path.join(CONFIG_DIR, "mcp.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history")

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
    """Encapsulates workspace path, active provider, credentials, flags, and conversation history."""

    workspace_dir: str = field(default_factory=lambda: os.path.realpath(os.getcwd()))
    rpc_mode: bool = False
    provider: str = "gemini"
    model: str = PROVIDERS["gemini"]["default_model"]
    thinking_level: str = "high"
    yolo_mode: bool = False
    session_summary: Optional[str] = None
    provider_overrides: Dict[str, Dict[str, str]] = field(default_factory=dict)
    saved_providers: Dict[str, Dict[str, str]] = field(default_factory=dict)
    gemini_client: Optional[Any] = None
    gemini_client_key: Optional[str] = None
    gemini_history: List[Any] = field(default_factory=list)
    universal_history: List[Dict[str, Any]] = field(default_factory=list)


state = AgentState()


def load_user_config() -> None:
    """Loads persistent defaults and per-provider credentials from ~/.termind/config.json."""
    if not os.path.isfile(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return

        raw_providers = data.get("providers") or {}
        if isinstance(raw_providers, dict):
            for p_name, p_cfg in raw_providers.items():
                if isinstance(p_cfg, dict):
                    clean_cfg: Dict[str, str] = {}
                    for k in ("api_key", "base_url", "model"):
                        if isinstance(p_cfg.get(k), str) and p_cfg[k].strip():
                            clean_cfg[k] = p_cfg[k].strip()
                    if clean_cfg:
                        state.saved_providers[p_name.lower()] = clean_cfg

        saved_provider = str(data.get("provider", "")).strip().lower()
        if saved_provider in PROVIDERS:
            state.provider = saved_provider
            state.model = (
                state.saved_providers.get(saved_provider, {}).get("model")
                or PROVIDERS[saved_provider]["default_model"]
            )

        if isinstance(data.get("model"), str) and data["model"].strip():
            state.model = data["model"].strip()

        saved_thinking = str(data.get("thinking_level", "")).strip().lower()
        if saved_thinking in ("off", "low", "medium", "high"):
            state.thinking_level = saved_thinking
    except Exception as e:
        print(f"\033[93m[Warning: Failed to parse {CONFIG_FILE}: {e}]\033[0m")


def save_user_config() -> None:
    """Auto-saves active provider, model, thinking level, and overrides to ~/.termind/config.json."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)

        merged_providers: Dict[str, Dict[str, str]] = copy.deepcopy(state.saved_providers)
        for p_name, overrides in state.provider_overrides.items():
            if overrides:
                merged_providers.setdefault(p_name, {}).update(overrides)
        merged_providers.setdefault(state.provider, {})["model"] = state.model

        # Drop empty provider sections after resets
        merged_providers = {k: v for k, v in merged_providers.items() if v}

        payload = {
            "provider": state.provider,
            "model": state.model,
            "thinking_level": state.thinking_level,
            "providers": merged_providers,
        }

        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")

        state.saved_providers = merged_providers
    except Exception as e:
        print(f"\033[93m[Warning: Failed to auto-save {CONFIG_FILE}: {e}]\033[0m")


def mask_secret(secret: str) -> str:
    """Masks API keys for safe display in /config output."""
    if not secret:
        return "(not set)"
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


def get_provider_api_key(provider_name: str) -> str:
    """Priority: session override -> environment variable -> ~/.termind/config.json."""
    override_key = state.provider_overrides.get(provider_name, {}).get("api_key")
    if override_key:
        return override_key
    env_var = PROVIDERS.get(provider_name, {}).get("api_key_env")
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return state.saved_providers.get(provider_name, {}).get("api_key", "")


def get_provider_base_url(provider_name: str) -> Optional[str]:
    """Priority: session override -> ~/.termind/config.json -> provider default."""
    override_url = state.provider_overrides.get(provider_name, {}).get("base_url")
    if override_url:
        return override_url
    saved_url = state.saved_providers.get(provider_name, {}).get("base_url")
    if saved_url:
        return saved_url
    return PROVIDERS.get(provider_name, {}).get("base_url")


def get_gemini_client() -> Any:
    """Returns a cached Google GenAI client, rebuilding automatically if the key changed."""
    from google import genai

    api_key = get_provider_api_key("gemini")
    if state.gemini_client is None or state.gemini_client_key != api_key:
        state.gemini_client = genai.Client(api_key=api_key or None)
        state.gemini_client_key = api_key
    return state.gemini_client

"""Optional Model Context Protocol (MCP v2) plugin manager."""

import asyncio
from contextlib import AsyncExitStack
import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    MAX_MCP_TOOL_NAME_LEN,
    MAX_SHELL_OUTPUT_CHARS,
    MCP_CALL_TIMEOUT_SEC,
    MCP_GLOBAL_CONFIG,
    state,
)
from .security import ask_user_permission


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
            (MCP_GLOBAL_CONFIG, "global", False),
            (os.path.join(state.workspace_dir, ".mcp.json"), "workspace", True),
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
                workspace_root = os.path.realpath(state.workspace_dir)
                cwd_path = os.path.realpath(os.path.join(workspace_root, raw_cwd)) if raw_cwd else workspace_root
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
                text_out = (
                    text_out[:4000]
                    + "\n... [MCP output truncated] ...\n"
                    + text_out[-(MAX_SHELL_OUTPUT_CHARS - 4000):]
                )
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
            return f"No MCP servers configured. Add servers to {MCP_GLOBAL_CONFIG} or ./.mcp.json."
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

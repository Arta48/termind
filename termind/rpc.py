"""Headless JSON-RPC 2.0 over NDJSON transport, cancellation, and permission synchronization."""

import json
import queue
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple
import uuid

from .config import state


_RPC_OUT = sys.stdout
_RPC_LOCK = threading.Lock()
_RPC_CMD_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_RPC_PERM_LOCK = threading.Lock()
_RPC_PERM_WAITERS: Dict[str, Tuple[threading.Event, Dict[str, bool]]] = {}
_RPC_CANCEL_EVENT = threading.Event()


def configure_rpc_stdio() -> None:
    """Reconfigures UTF-8 stdio and redirects sys.stdout to stderr so stdout stays pure NDJSON."""
    if hasattr(_RPC_OUT, "reconfigure"):
        _RPC_OUT.reconfigure(encoding="utf-8", line_buffering=True)
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout = sys.stderr


def rpc_emit(payload: Dict[str, Any]) -> None:
    """Writes a single thread-safe JSON-RPC 2.0 NDJSON line to stdout in --rpc mode."""
    if not state.rpc_mode:
        return

    if "jsonrpc" in payload:
        msg = payload
    else:
        m_type = payload.get("type", "")
        if m_type == "permission_request":
            # Server -> Client Request (expects JSON-RPC response with matching id)
            msg = {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "method": "permission_request",
                "params": {
                    "action": payload.get("action", ""),
                    "details": payload.get("details", ""),
                },
            }
        else:
            # Server -> Client Notification
            params = {k: v for k, v in payload.items() if k != "type"}
            msg = {
                "jsonrpc": "2.0",
                "method": m_type,
                "params": params,
            }

    line = json.dumps(msg, ensure_ascii=False)
    with _RPC_LOCK:
        _RPC_OUT.write(line + "\n")
        _RPC_OUT.flush()


def _send_rpc_error(req_id: Optional[Any], code: int, message: str) -> None:
    rpc_emit({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": code,
            "message": message,
        },
    })


def is_rpc_cancelled() -> bool:
    """Returns True if --rpc mode is active and cancellation has been requested."""
    return state.rpc_mode and _RPC_CANCEL_EVENT.is_set()


def clear_rpc_cancel() -> None:
    """Clears the cancellation flag before starting a new RPC turn."""
    _RPC_CANCEL_EVENT.clear()


def wait_rpc_cancel(timeout: float) -> bool:
    """Waits up to `timeout` seconds for an RPC cancellation signal."""
    return _RPC_CANCEL_EVENT.wait(timeout=timeout)


def rpc_check_cancelled() -> None:
    """Raises KeyboardInterrupt if the GUI requested cancellation."""
    if state.rpc_mode and _RPC_CANCEL_EVENT.is_set():
        raise KeyboardInterrupt("Cancelled by RPC client")


def rpc_interruptible_sleep(seconds: float) -> None:
    """Sleeps for `seconds`, aborting immediately on RPC cancellation."""
    if state.rpc_mode:
        if _RPC_CANCEL_EVENT.wait(timeout=seconds):
            raise KeyboardInterrupt("Cancelled by RPC client")
    else:
        time.sleep(seconds)


def rpc_request_permission(action: str, details: str = "") -> bool:
    """Sends a permission_request over NDJSON and blocks until the GUI replies or cancels."""
    req_id = uuid.uuid4().hex[:8]
    ev = threading.Event()
    box = {"allow": False}
    with _RPC_PERM_LOCK:
        if _RPC_CANCEL_EVENT.is_set():
            return False
        _RPC_PERM_WAITERS[req_id] = (ev, box)
    rpc_emit({
        "type": "permission_request",
        "id": req_id,
        "action": action,
        "details": details[:4000],
    })
    ev.wait()
    with _RPC_PERM_LOCK:
        _RPC_PERM_WAITERS.pop(req_id, None)
    return bool(box["allow"]) and not _RPC_CANCEL_EVENT.is_set()


def emit_turn_error(message: str) -> None:
    """Reports a turn-level error via RPC or colored terminal output."""
    if state.rpc_mode:
        rpc_emit({"type": "error", "message": message})
    else:
        print(f"\n\033[91m[{message}]\033[0m")


def emit_turn_notice(message: str) -> None:
    """Reports a turn-level notice via RPC or colored terminal output."""
    if state.rpc_mode:
        rpc_emit({"type": "notice", "text": message})
    else:
        print(f"\n\033[93m[{message}]\033[0m")


def _rpc_stdin_reader() -> None:
    """Reads incoming strict JSON-RPC 2.0 messages from stdin."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send_rpc_error(None, -32700, "Parse error")
            continue

        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            _send_rpc_error(None, -32600, "Invalid Request: 'jsonrpc' must be '2.0'")
            continue

        # Standard JSON-RPC 2.0 Response (reply from client to permission_request)
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = str(msg["id"])
            res = msg.get("result", {})
            allow = False
            if isinstance(res, dict):
                allow = bool(res.get("allow", False))
            elif isinstance(res, bool):
                allow = res
            with _RPC_PERM_LOCK:
                waiter = _RPC_PERM_WAITERS.get(req_id)
            if waiter:
                waiter[1]["allow"] = allow
                waiter[0].set()
            continue

        # Standard JSON-RPC 2.0 Request or Notification
        method = msg.get("method")
        if not isinstance(method, str) or not method:
            _send_rpc_error(msg.get("id"), -32600, "Invalid Request: missing 'method'")
            continue

        params = msg.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            _send_rpc_error(msg.get("id"), -32602, "Invalid params: object expected")
            continue

        if method == "cancel":
            _RPC_CANCEL_EVENT.set()
            with _RPC_PERM_LOCK:
                for ev, box in _RPC_PERM_WAITERS.values():
                    box["allow"] = False
                    ev.set()
        else:
            cmd_item = {"cmd": method, **params}
            _RPC_CMD_QUEUE.put(cmd_item)

    # Stdin closed by parent GUI -> cancel & exit cleanly
    _RPC_CANCEL_EVENT.set()
    with _RPC_PERM_LOCK:
        for ev, box in _RPC_PERM_WAITERS.values():
            box["allow"] = False
            ev.set()
    _RPC_CMD_QUEUE.put({"cmd": "quit"})


def start_rpc_stdin_reader() -> threading.Thread:
    """Starts the background daemon thread reading NDJSON commands from stdin."""
    reader_thread = threading.Thread(target=_rpc_stdin_reader, daemon=True, name="termind-rpc-in")
    reader_thread.start()
    return reader_thread


def get_rpc_command() -> Dict[str, Any]:
    """Blocks until the next queued RPC command arrives from stdin."""
    return _RPC_CMD_QUEUE.get()

"""Live-streaming cross-platform shell execution tool."""

import os
import re
import signal
import subprocess
import threading
from typing import Any, Dict, List, Optional, Union

from ..config import DANGEROUS_COMMANDS, IS_WINDOWS, MAX_SHELL_OUTPUT_CHARS, OS_NAME, state
from ..rpc import is_rpc_cancelled, rpc_check_cancelled, rpc_emit, wait_rpc_cancel
from ..security import ask_user_permission


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

    cancel_stop = threading.Event()

    def _kill_process_tree(is_timeout: bool = False) -> None:
        if process is None or process.poll() is not None:
            return
        if is_timeout:
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

    def _watch_rpc_cancel() -> None:
        while not cancel_stop.is_set():
            if wait_rpc_cancel(0.25):
                _kill_process_tree(is_timeout=False)
                return

    timer = threading.Timer(timeout_sec, _kill_process_tree, kwargs={"is_timeout": True})
    cancel_thread: Optional[threading.Thread] = None

    try:
        if not state.rpc_mode:
            print(f"\n\033[94m[Running command ({OS_NAME})]: $ {command}\033[0m")

        popen_kwargs: Dict[str, Any] = {
            "cwd": state.workspace_dir,
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
        if state.rpc_mode:
            cancel_thread = threading.Thread(target=_watch_rpc_cancel, daemon=True)
            cancel_thread.start()

        output_lines: List[str] = []
        if process.stdout:
            rpc_emit({"type": "shell_start", "command": command})
            try:
                for line in process.stdout:
                    if state.rpc_mode:
                        if is_rpc_cancelled():
                            _kill_process_tree(is_timeout=False)
                            break
                        rpc_emit({"type": "shell_output", "line": line})
                    else:
                        print(line, end="", flush=True)
                    output_lines.append(line)
            finally:
                rpc_emit({"type": "shell_end", "command": command})

        process.wait()
        rpc_check_cancelled()

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
    except KeyboardInterrupt:
        raise
    except Exception as e:
        return f"Error executing command: {e}"
    finally:
        timer.cancel()
        cancel_stop.set()
        if cancel_thread is not None and cancel_thread.is_alive():
            cancel_thread.join(timeout=0.5)

"""Live-streaming cross-platform shell execution tool."""

import os
import re
import signal
import subprocess
import threading
from typing import Any, Dict, List, Optional, Union

from ..config import DANGEROUS_COMMANDS, IS_WINDOWS, MAX_SHELL_OUTPUT_CHARS, OS_NAME, state
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

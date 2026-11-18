"""Workspace sandboxing, Human-in-the-Loop prompts, and SSRF protection."""

import ipaddress
import os
import socket
from typing import Any, Optional, Tuple
import urllib.parse
import urllib.request

from .config import BINARY_EXTENSIONS, IMAGE_EXTENSIONS, state


def check_safe_path(file_path: str) -> Tuple[bool, str]:
    """Validates that the resolved path (including symlinks) remains inside the workspace sandbox."""
    if not file_path or not file_path.strip():
        return False, "Access blocked: empty path provided."
    workspace_root = os.path.realpath(state.workspace_dir)
    resolved_target = os.path.realpath(os.path.join(workspace_root, file_path))
    try:
        if os.path.commonpath([workspace_root, resolved_target]) != workspace_root:
            return False, f"Access blocked: path '{file_path}' resolves outside the workspace sandbox."
    except ValueError:
        return False, f"Access blocked: path '{file_path}' is located on a different drive."
    return True, resolved_target


def is_within_workspace(abs_path: str) -> bool:
    """Returns True if abs_path resolves inside the workspace after symlink resolution."""
    workspace_root = os.path.realpath(state.workspace_dir)
    resolved = os.path.realpath(abs_path)
    try:
        return os.path.commonpath([workspace_root, resolved]) == workspace_root
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

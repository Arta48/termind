"""Built-in tool exports, OpenAI-compatible JSON schemas, and unified tool dispatcher."""

from typing import Any, Dict, List

from ..mcp_client import mcp_manager
from .fs import (
    delete_file,
    edit_file_replace,
    list_directory,
    read_file,
    search_code,
    write_file,
)
from .shell import execute_shell_command
from .vision import view_image
from .web import fetch_webpage_content


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


__all__ = [
    "NATIVE_TOOLS_LIST",
    "TOOLS_MAP",
    "TOOLS_SCHEMAS",
    "delete_file",
    "edit_file_replace",
    "execute_shell_command",
    "execute_tool_call",
    "fetch_webpage_content",
    "list_directory",
    "read_file",
    "search_code",
    "view_image",
    "write_file",
]

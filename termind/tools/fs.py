"""Workspace-sandboxed filesystem and code search tools."""

import os
from typing import List

from ..config import (
    IMAGE_EXTENSIONS,
    MAX_LIST_DIR_ITEMS,
    MAX_READ_LINE_CHARS,
    MAX_READ_LINES,
    MAX_READ_OUTPUT_CHARS,
    MAX_SEARCH_FILE_BYTES,
    state,
)
from ..security import (
    ask_user_permission,
    check_safe_path,
    is_binary_file,
    is_within_workspace,
)


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

            rel_path = os.path.relpath(full_path, state.workspace_dir)
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

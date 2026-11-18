"""Anthropic (Claude) native engine with Extended Thinking and signature preservation."""

import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..config import state
from .streaming import StepResult, StreamRenderer, is_bad_request_error


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


def stream_anthropic_step(
    renderer: StreamRenderer,
    active_schemas: List[Dict[str, Any]],
    allow_tools: bool,
    api_key: str,
) -> StepResult:
    """Streams a single completion step via the native Anthropic Messages SDK."""
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

    tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
    usage_stats: Optional[Tuple[int, int, int]] = None
    anthropic_blocks: Optional[List[Dict[str, Any]]] = None

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

    return StepResult(
        full_text=renderer.full_text,
        anthropic_blocks=anthropic_blocks,
        tool_calls_buffer=tool_calls_buffer,
        usage_stats=usage_stats,
    )

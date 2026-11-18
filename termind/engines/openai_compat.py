"""OpenAI-compatible engine (OpenAI, DeepSeek, Groq, Ollama, OpenRouter)."""

from typing import Any, Dict, List, Optional, Tuple

from ..config import state
from ..rpc import rpc_check_cancelled
from .streaming import (
    StepResult,
    StreamRenderer,
    ThinkTagStreamParser,
    is_bad_request_error,
)


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


def stream_openai_step(
    renderer: StreamRenderer,
    active_schemas: List[Dict[str, Any]],
    allow_tools: bool,
    api_key: str,
    base_url: Optional[str],
) -> StepResult:
    """Streams a single completion step via an OpenAI-compatible Chat Completions API."""
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

    reasoning_chunks: List[str] = []
    has_native_reasoning = False
    tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
    usage_stats: Optional[Tuple[int, int, int]] = None
    think_parser = ThinkTagStreamParser()

    for chunk in stream:
        rpc_check_cancelled()
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

    return StepResult(
        full_text=renderer.full_text,
        reasoning_text="".join(reasoning_chunks),
        has_native_reasoning=has_native_reasoning,
        tool_calls_buffer=tool_calls_buffer,
        usage_stats=usage_stats,
    )

"""Google Gemini native engine with ThinkingConfig, thought streaming, and signature preservation."""

import copy
import time
from typing import Any, Dict, List, Optional

from ..config import MAX_TOOL_STEPS, get_gemini_client, state
from ..mcp_client import mcp_manager
from ..session import build_system_prompt
from ..tools import NATIVE_TOOLS_LIST, execute_tool_call
from .streaming import (
    StreamRenderer,
    is_bad_request_error,
    is_retryable_error,
    print_token_usage,
)


def build_gemini_thinking_config(types_mod: Any, model_name: str, level: str) -> Optional[Any]:
    """Selects model-compatible ThinkingConfig (thinking_budget for 2.5, thinking_level for 3+/4)."""
    m = model_name.lower()
    if "gemini-1." in m or "gemini-2.0" in m:
        return None
    if "gemini-2.5" in m:
        if level == "off":
            budget = 128 if "pro" in m else 0
        elif level == "low":
            budget = 2048
        elif level == "medium":
            budget = 6144
        else:
            budget = -1
        return types_mod.ThinkingConfig(
            thinking_budget=budget,
            include_thoughts=(level != "off"),
        )

    # Models like gemini-3.8-flash and gemini-4-* reject "minimal" and only accept low/medium/high
    supports_minimal = any(
        tag in m for tag in ("gemini-3-flash", "gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-3.6-flash", "robotics")
    )
    effective_level = ("minimal" if supports_minimal else "low") if level == "off" else level

    return types_mod.ThinkingConfig(
        thinking_level=effective_level,
        include_thoughts=(level != "off"),
    )


def build_gemini_user_content(types_mod: Any, user_content: Any) -> Any:
    """Converts string or multimodal list payloads into a google-genai Content object."""
    if isinstance(user_content, str):
        parts = [types_mod.Part(text=user_content)]
    elif isinstance(user_content, list):
        parts = [
            types_mod.Part(text=item) if isinstance(item, str) else item
            for item in user_content
        ]
    else:
        parts = [user_content]
    return types_mod.Content(role="user", parts=parts)


def merge_gemini_stream_part(collected_parts: List[Any], part: Any) -> None:
    """Appends or merges a streamed Gemini Part while strictly preserving all thought signatures."""
    part_copy = part.model_copy() if hasattr(part, "model_copy") else copy.copy(part)

    has_structured_payload = any(
        getattr(part_copy, attr, None) is not None
        for attr in ("function_call", "function_response", "inline_data", "file_data", "executable_code", "code_execution_result")
    )
    if has_structured_payload:
        collected_parts.append(part_copy)
        return

    incoming_text = getattr(part_copy, "text", None) or ""
    incoming_sig = getattr(part_copy, "thought_signature", None)
    incoming_thought = bool(getattr(part_copy, "thought", False))

    if not incoming_text and incoming_sig is not None:
        if collected_parts and getattr(collected_parts[-1], "thought_signature", None) is None:
            collected_parts[-1].thought_signature = incoming_sig
        else:
            collected_parts.append(part_copy)
        return

    if not incoming_text and incoming_sig is None:
        return

    if collected_parts:
        prev = collected_parts[-1]
        prev_is_structured = any(
            getattr(prev, attr, None) is not None
            for attr in ("function_call", "function_response", "inline_data", "file_data", "executable_code", "code_execution_result")
        )
        prev_sig = getattr(prev, "thought_signature", None)
        can_merge = (
            not prev_is_structured
            and bool(getattr(prev, "thought", False)) == incoming_thought
            and (prev_sig is None or incoming_sig is None)
        )
        if can_merge:
            prev.text = (getattr(prev, "text", None) or "") + incoming_text
            if incoming_sig is not None:
                prev.thought_signature = incoming_sig
            return

    collected_parts.append(part_copy)


def run_gemini_turn(user_content: Any, allow_tools: bool = True) -> str:
    """Executes a multi-step Gemini turn with manual tool handling and preserved thought signatures."""
    from google.genai import types

    try:
        client = get_gemini_client()
    except Exception as e:
        print(f"\n\033[91m[Error initializing Gemini client: {e}]\033[0m")
        return ""

    turn_start_idx = len(state.gemini_history)
    state.gemini_history.append(build_gemini_user_content(types, user_content))

    max_retries = 6
    base_delay = 2
    final_answer = ""

    try:
        for _step in range(MAX_TOOL_STEPS):
            thinking_cfg = build_gemini_thinking_config(types, state.model, state.thinking_level)
            mcp_decls = mcp_manager.get_gemini_declarations(types)
            gemini_tools: List[Any]
            if mcp_decls:
                native_decls = [
                    types.FunctionDeclaration.from_callable(
                        client=client,
                        callable=fn,
                        use_json_schema=True,
                    )
                    for fn in NATIVE_TOOLS_LIST
                ]
                gemini_tools = [types.Tool(function_declarations=native_decls + mcp_decls)]
            else:
                gemini_tools = NATIVE_TOOLS_LIST

            cfg_kwargs: Dict[str, Any] = {
                "system_instruction": build_system_prompt(),
                "tools": gemini_tools,
                "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            }
            if thinking_cfg is not None:
                cfg_kwargs["thinking_config"] = thinking_cfg
            if not allow_tools:
                cfg_kwargs["tool_config"] = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="NONE")
                )

            collected_parts: List[Any] = []
            last_usage = None
            step_succeeded = False

            for attempt in range(max_retries):
                renderer = StreamRenderer()
                collected_parts = []
                last_usage = None

                try:
                    cfg = types.GenerateContentConfig(**cfg_kwargs)
                    response_stream = client.models.generate_content_stream(
                        model=state.model,
                        contents=state.gemini_history,
                        config=cfg,
                    )

                    for chunk in response_stream:
                        if getattr(chunk, "usage_metadata", None):
                            last_usage = chunk.usage_metadata
                        if not chunk.candidates or not chunk.candidates[0].content:
                            continue

                        for part in chunk.candidates[0].content.parts or []:
                            merge_gemini_stream_part(collected_parts, part)

                            if getattr(part, "function_call", None):
                                renderer.stop_live()
                                continue
                            if not getattr(part, "text", None):
                                continue

                            if getattr(part, "thought", False):
                                renderer.emit_thought(part.text)
                            else:
                                renderer.emit_text(part.text)

                    renderer.finish_step()
                    step_succeeded = True
                    break

                except KeyboardInterrupt:
                    renderer.finish_step()
                    raise
                except Exception as e:
                    renderer.finish_step()
                    if "thinking_config" in cfg_kwargs and is_bad_request_error(e):
                        cfg_kwargs.pop("thinking_config", None)
                        continue
                    if is_retryable_error(e) and attempt < max_retries - 1:
                        sleep_s = min(60, base_delay * (2 ** attempt))
                        print(f"\n\033[93m[Server busy. Retrying in {sleep_s}s... Attempt {attempt + 1}/{max_retries}]\033[0m")
                        time.sleep(sleep_s)
                        continue
                    print(f"\n\033[91m[Execution Error]: {e}\033[0m")
                    del state.gemini_history[turn_start_idx:]
                    return ""

            if not step_succeeded:
                del state.gemini_history[turn_start_idx:]
                return ""

            if last_usage:
                print_token_usage(
                    getattr(last_usage, "prompt_token_count", 0) or 0,
                    getattr(last_usage, "candidates_token_count", 0) or 0,
                    getattr(last_usage, "total_token_count", 0) or 0,
                )

            if collected_parts:
                state.gemini_history.append(types.Content(role="model", parts=collected_parts))

            function_calls = [
                p.function_call for p in collected_parts if getattr(p, "function_call", None) is not None
            ]

            if not function_calls or not allow_tools:
                final_answer = "".join(
                    p.text for p in collected_parts
                    if getattr(p, "text", None) and not getattr(p, "thought", False)
                )
                break

            response_parts: List[Any] = []
            for fc in function_calls:
                fn_args = dict(fc.args) if getattr(fc, "args", None) else {}
                tool_result = execute_tool_call(fc.name, fn_args)
                fr_part = types.Part.from_function_response(
                    name=fc.name,
                    response={"result": tool_result or "(no output)"},
                )
                if getattr(fc, "id", None) and getattr(fr_part, "function_response", None) is not None:
                    fr_part.function_response.id = fc.id
                response_parts.append(fr_part)

            state.gemini_history.append(types.Content(role="user", parts=response_parts))
        else:
            print(f"\n\033[93m[Stopped after reaching {MAX_TOOL_STEPS} tool steps]\033[0m")

    except KeyboardInterrupt:
        print("\n\033[93m[Generation interrupted by user]\033[0m")
        del state.gemini_history[turn_start_idx:]
        return ""

    return final_answer

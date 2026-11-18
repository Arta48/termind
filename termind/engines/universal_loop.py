"""Shared multi-step tool loop, retries, and history rollback for OpenAI/Anthropic engines."""

import json
import time
from typing import Any, Dict, Optional
import uuid

from ..config import (
    MAX_TOOL_STEPS,
    PROVIDERS,
    get_provider_api_key,
    get_provider_base_url,
    state,
)
from ..mcp_client import mcp_manager
from ..tools import TOOLS_SCHEMAS, execute_tool_call
from .anthropic import stream_anthropic_step
from .openai_compat import stream_openai_step
from .streaming import StepResult, StreamRenderer, is_retryable_error, print_token_usage


def run_openai_or_anthropic_turn(user_content: Any, allow_tools: bool = True) -> str:
    """Executes a multi-turn tool loop via OpenAI-compatible or Anthropic SDKs."""
    turn_start_idx = len(state.universal_history)
    state.universal_history.append({"role": "user", "content": user_content})

    p_info = PROVIDERS.get(state.provider, PROVIDERS["openai"])
    p_engine = p_info["engine"]
    api_key = get_provider_api_key(state.provider)
    base_url = get_provider_base_url(state.provider)

    max_retries = 5
    base_delay = 2
    final_answer = ""

    try:
        for _step in range(MAX_TOOL_STEPS):
            active_schemas = TOOLS_SCHEMAS + mcp_manager.get_openai_schemas()
            attempt_succeeded = False
            step_result: Optional[StepResult] = None

            for attempt in range(max_retries):
                renderer = StreamRenderer()
                try:
                    if p_engine == "openai":
                        step_result = stream_openai_step(
                            renderer=renderer,
                            active_schemas=active_schemas,
                            allow_tools=allow_tools,
                            api_key=api_key,
                            base_url=base_url,
                        )
                    elif p_engine == "anthropic":
                        step_result = stream_anthropic_step(
                            renderer=renderer,
                            active_schemas=active_schemas,
                            allow_tools=allow_tools,
                            api_key=api_key,
                        )
                    else:
                        raise ValueError(f"Unsupported engine '{p_engine}' for provider '{state.provider}'.")

                    renderer.finish_step()
                    attempt_succeeded = True
                    break

                except KeyboardInterrupt:
                    renderer.finish_step()
                    raise
                except Exception as e:
                    renderer.finish_step()
                    if is_retryable_error(e) and attempt < max_retries - 1:
                        sleep_s = min(60, base_delay * (2 ** attempt))
                        print(f"\n\033[93m[Server busy. Retrying in {sleep_s}s... Attempt {attempt + 1}/{max_retries}]\033[0m")
                        time.sleep(sleep_s)
                        continue

                    print(f"\n\033[91m[Execution Error]: {e}\033[0m")
                    del state.universal_history[turn_start_idx:]
                    return ""

            if not attempt_succeeded or step_result is None:
                del state.universal_history[turn_start_idx:]
                return ""

            if step_result.usage_stats:
                print_token_usage(*step_result.usage_stats)

            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": step_result.full_text,
            }
            if step_result.reasoning_text or step_result.has_native_reasoning:
                assistant_msg["reasoning_content"] = step_result.reasoning_text
                assistant_msg["_has_native_reasoning"] = True
            if step_result.anthropic_blocks is not None:
                assistant_msg["anthropic_blocks"] = step_result.anthropic_blocks

            if not step_result.tool_calls_buffer or not allow_tools:
                if step_result.full_text or step_result.anthropic_blocks:
                    state.universal_history.append(assistant_msg)
                final_answer = step_result.full_text
                break

            assistant_tool_calls = []
            for idx in sorted(step_result.tool_calls_buffer.keys()):
                tc_data = step_result.tool_calls_buffer[idx]
                assistant_tool_calls.append({
                    "id": tc_data["id"] or f"call_{idx}_{uuid.uuid4().hex[:8]}",
                    "type": "function",
                    "function": {
                        "name": tc_data["name"],
                        "arguments": tc_data["arguments"],
                    },
                })
            assistant_msg["tool_calls"] = assistant_tool_calls
            state.universal_history.append(assistant_msg)

            for tc in assistant_tool_calls:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"]["arguments"]
                try:
                    fn_args = json.loads(raw_args) if raw_args else {}
                except json.JSONDecodeError as e:
                    tool_result = (
                        f"Error: malformed JSON arguments for tool '{fn_name}': {e}. "
                        f"Raw arguments received: {raw_args!r}"
                    )
                else:
                    tool_result = execute_tool_call(fn_name, fn_args)

                state.universal_history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": tool_result or "(no output)",
                })
        else:
            print(f"\n\033[93m[Stopped after reaching {MAX_TOOL_STEPS} tool steps]\033[0m")

    except KeyboardInterrupt:
        print("\n\033[93m[Generation interrupted by user]\033[0m")
        del state.universal_history[turn_start_idx:]
        return ""

    return final_answer

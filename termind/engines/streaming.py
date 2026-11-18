"""Streaming renderers, <think> tag parser, token usage display, and API error classifiers."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markdown import Markdown

    console: Optional[Console] = Console()
    HAS_RICH = True
except ImportError:
    console = None
    HAS_RICH = False


@dataclass
class StepResult:
    """Normalized output of a single streamed LLM step."""

    full_text: str
    reasoning_text: str = ""
    has_native_reasoning: bool = False
    anthropic_blocks: Optional[List[Dict[str, Any]]] = None
    tool_calls_buffer: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    usage_stats: Optional[Tuple[int, int, int]] = None


class ThinkTagStreamParser:
    """Splits inline <think>...</think> blocks cleanly across arbitrary stream chunk boundaries."""

    def __init__(self) -> None:
        self.in_think = False
        self.buffer = ""

    def feed(self, text: str) -> List[Tuple[bool, str]]:
        """Returns a list of (is_thought, segment_text) tuples."""
        self.buffer += text
        events: List[Tuple[bool, str]] = []

        while self.buffer:
            tag = "</think>" if self.in_think else "<think>"
            idx = self.buffer.find(tag)
            if idx != -1:
                if idx > 0:
                    events.append((self.in_think, self.buffer[:idx]))
                self.in_think = not self.in_think
                self.buffer = self.buffer[idx + len(tag):]
                continue

            # Hold back any trailing prefix of the target tag to prevent split-tag leakage
            hold = 0
            max_prefix = min(len(tag) - 1, len(self.buffer))
            for length in range(max_prefix, 0, -1):
                if tag.startswith(self.buffer[-length:]):
                    hold = length
                    break

            emit_len = len(self.buffer) - hold
            if emit_len > 0:
                events.append((self.in_think, self.buffer[:emit_len]))
                self.buffer = self.buffer[emit_len:]
            break

        return events

    def flush(self) -> List[Tuple[bool, str]]:
        if not self.buffer:
            return []
        rem = self.buffer
        self.buffer = ""
        return [(self.in_think, rem)]


class StreamRenderer:
    """Coordinates terminal output for reasoning streams and live Markdown responses."""

    def __init__(self) -> None:
        self.in_thought = False
        self.full_text = ""
        self.live: Optional[Any] = None

    def emit_thought(self, text: str) -> None:
        if not text:
            return
        self.stop_live()
        if not self.in_thought:
            print("\n\033[90m[Agent Reasoning]:\033[0m")
            self.in_thought = True
        print(f"\033[90m{text}\033[0m", end="", flush=True)

    def emit_text(self, text: str) -> None:
        if not text:
            return
        if self.in_thought:
            print()
            self.in_thought = False
        if self.live is None:
            if HAS_RICH and console is not None:
                console.print("\n[bold cyan]🤖 [Agent]:[/bold cyan]")
                self.live = Live(console=console, refresh_per_second=12)
                self.live.start()
            else:
                print("\n\033[1m[Agent]:\033[0m\n", end="", flush=True)
        self.full_text += text
        if self.live is not None:
            self.live.update(Markdown(self.full_text))
        else:
            print(text, end="", flush=True)

    def stop_live(self) -> None:
        if self.live is not None:
            try:
                self.live.stop()
            finally:
                self.live = None

    def finish_step(self) -> None:
        if self.in_thought:
            print()
            self.in_thought = False
        self.stop_live()
        if self.full_text and not HAS_RICH:
            print()


def print_token_usage(prompt_tokens: int, output_tokens: int, total_tokens: int) -> None:
    """Prints token counts in dimmed style."""
    meta_str = f"Tokens: prompt: {prompt_tokens} | output: {output_tokens} | total: {total_tokens}"
    if HAS_RICH and console is not None:
        console.print(f"[dim]{meta_str}[/dim]")
    else:
        print(f"\033[90m{meta_str}\033[0m")


def is_retryable_error(err: Exception) -> bool:
    """Detects transient rate-limit and service-unavailable API errors."""
    status = getattr(err, "status_code", None)
    if status in (429, 503, 529):
        return True
    err_str = str(err).lower()
    return any(k in err_str for k in ("503", "429", "529", "unavailable", "resource_exhausted", "overloaded"))


def is_bad_request_error(err: Exception) -> bool:
    """Detects HTTP 400/422 validation errors triggered by unsupported optional parameters."""
    status = getattr(err, "status_code", None)
    if status in (400, 422):
        return True
    err_str = str(err)
    return "400" in err_str or "422" in err_str

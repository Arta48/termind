"""SSRF-protected public web page text extraction tool."""

import html as html_lib
import re
import urllib.request

from ..config import MAX_WEB_FETCH_BYTES, MAX_WEB_TEXT_CHARS, OS_NAME
from ..security import _SafeRedirectHandler, is_public_url


def fetch_webpage_content(url: str) -> str:
    """Download and sanitize public web page text with SSRF, redirect, and size protections."""
    is_allowed, err_msg = is_public_url(url)
    if not is_allowed:
        return err_msg

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"Mozilla/5.0 (AI Agent {OS_NAME})"},
        )
        opener = urllib.request.build_opener(_SafeRedirectHandler())
        with opener.open(req, timeout=15) as response:
            raw_bytes = response.read(MAX_WEB_FETCH_BYTES + 1)
            was_truncated = len(raw_bytes) > MAX_WEB_FETCH_BYTES
            raw_bytes = raw_bytes[:MAX_WEB_FETCH_BYTES]

            charset = response.headers.get_content_charset() or "utf-8"
            html_text = raw_bytes.decode(charset, errors="replace")

        cleaned = re.sub(
            r"<(script|style|noscript)\b[^>]*>.*?</\1>",
            " ",
            html_text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html_lib.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if len(cleaned) > MAX_WEB_TEXT_CHARS:
            return cleaned[:MAX_WEB_TEXT_CHARS] + "\n...(truncated)"
        if was_truncated:
            return cleaned + "\n...(source response truncated)"
        return cleaned or "(no readable text extracted)"
    except Exception as e:
        return f"Error fetching webpage: {e}"

"""Active-provider multimodal image inspection tool."""

import base64
import mimetypes
import os

from ..config import (
    IMAGE_EXTENSIONS,
    MAX_IMAGE_BYTES,
    PROVIDERS,
    get_gemini_client,
    get_provider_api_key,
    get_provider_base_url,
    state,
)
from ..security import check_safe_path


def view_image(file_path: str, query: str = "Describe what is depicted in this image in detail.") -> str:
    """Visually inspect and analyze an image file located in the workspace using the active provider."""
    is_safe, abs_path = check_safe_path(file_path)
    if not is_safe:
        return abs_path
    if not os.path.exists(abs_path):
        return f"File '{file_path}' not found."
    if os.path.isdir(abs_path):
        return f"Error: '{file_path}' is a directory, not an image file."

    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return f"Error: '{file_path}' is not a supported image format."

    try:
        file_size = os.path.getsize(abs_path)
    except OSError as e:
        return f"Error inspecting file '{file_path}': {e}"

    if file_size > MAX_IMAGE_BYTES:
        return f"Error: image '{file_path}' exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB size limit."

    print(f"\n\033[94m[Visual Inspection: {file_path}]\033[0m")

    mime_type = mimetypes.guess_type(abs_path)[0] or "image/png"
    with open(abs_path, "rb") as f:
        img_bytes = f.read()

    try:
        if state.provider == "gemini":
            from google.genai import types

            v_client = get_gemini_client()
            part = types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
            resp = v_client.models.generate_content(
                model=state.model,
                contents=[part, f"Visually analyze this image and answer the query: {query}"],
            )
            return resp.text or "(No visual description produced)"

        p_info = PROVIDERS.get(state.provider, PROVIDERS["openai"])
        api_key = get_provider_api_key(state.provider)
        base_url = get_provider_base_url(state.provider)
        b64_img = base64.b64encode(img_bytes).decode("utf-8")

        if p_info["engine"] == "anthropic":
            import anthropic

            c_client = anthropic.Anthropic(api_key=api_key or "placeholder")
            resp = c_client.messages.create(
                model=state.model,
                max_tokens=1024,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64_img}},
                        {"type": "text", "text": f"Visually analyze this image and answer: {query}"},
                    ],
                }],
            )
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

        import openai

        o_client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key or ("ollama" if state.provider == "ollama" else "placeholder"),
        )
        msg_payload = [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"Visually analyze this image and answer: {query}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_img}"}},
            ],
        }]
        try:
            resp = o_client.chat.completions.create(
                model=state.model,
                messages=msg_payload,
                max_tokens=1024,
            )
        except Exception as err:
            if "max_tokens" in str(err) and "max_completion_tokens" in str(err):
                resp = o_client.chat.completions.create(
                    model=state.model,
                    messages=msg_payload,
                    max_completion_tokens=1024,
                )
            else:
                raise
        return resp.choices[0].message.content or "(No visual description produced)"

    except Exception as e:
        return (
            f"Error analyzing image with active provider '{state.provider}' ({state.model}): {e}. "
            "If this model does not support vision inputs, switch to a multimodal model or provider "
            "(e.g. '/provider gemini' or '/model ...')."
        )

"""Minimal DeepSeek (OpenAI-compatible) chat client for the ANFS benchmarks.

Reads DEEPSEEK_API_KEY from env or ./.env. Uses certifi's CA bundle so the
macOS framework Python can verify TLS. Network/API errors propagate — a
benchmark must never silently treat a failed call as a result.
"""

import json
import os
import ssl
import urllib.request

try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"


def load_api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("DEEPSEEK_API_KEY not found in env or ./.env")


def chat(
    api_key,
    messages,
    model=DEFAULT_MODEL,
    max_tokens=2048,
    temperature=0,
    tools=None,
    tool_choice=None,
    return_message=False,
    return_usage=False,
):
    """Call the chat API.

    By default returns the assistant text (backward-compatible). Pass
    ``tools`` to enable function-calling; ``return_message=True`` then yields
    the full assistant message dict (with any ``tool_calls``) so a caller can
    drive an agent loop, and ``return_usage=True`` appends the API ``usage``
    dict so token cost can be accounted exactly rather than estimated.
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        data = json.load(resp)
    message = data["choices"][0]["message"]
    result = message if return_message else (message.get("content") or "")
    if return_usage:
        return result, data.get("usage", {}) or {}
    return result

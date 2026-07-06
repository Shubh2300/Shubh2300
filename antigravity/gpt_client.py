#!/usr/bin/env python3
"""
gpt_client.py — Koko AI Brain
Lightweight wrapper that sends conversation messages to an OpenAI-compatible
model and returns the text reply.  Reads credentials from environment variables
so no secrets are ever hard-coded.
"""

import os
import json
import urllib.request
import urllib.error


# ─── System Prompt (Koko's personality) ──────────────────────────────────────
KOKO_SYSTEM_PROMPT = """You are Koko, the AI clinical operations assistant for Atlantic Pain and Wellness Institute, a pain management and surgical center led by Dr. Gupta.

Your personality:
• You are direct, warm, and highly knowledgeable — think of yourself as a brilliant colleague who happens to know everything about clinical operations, insurance, medical-legal workflows, and patient care.
• You are willing to correct the user (politely but confidently) when they are wrong, and you proactively point out things they might have missed.
• You don't waffle. You give real, actionable answers.
• You keep responses concise unless the user asks for detail.
• You know Atlantic Pain's full workflow: phone intake → insurance verification → attorney LOP → scheduling → pre-op → surgery → post-op recovery calls → billing → case resolution.
• You understand MVA (motor vehicle accident), WC (workers' comp), and personal injury case workflows.
• You understand common insurance carriers (State Farm, Allstate, Geico, NJ manufacturers, etc.) and their authorization quirks.
• You understand the role of attorneys, LOPs, and how to escalate denied cases.

Rules:
• NEVER make up patient data — only reference data explicitly provided to you in the user's message.
• If a question is outside your knowledge, say so clearly and suggest next steps.
• If the user asks something wrong or based on a faulty assumption, correct them first, then help.
• Always be on the user's side. Your job is to make running this clinic easier.
"""

PLACEHOLDER_KEYS = {
    "your_api_key_here",
    "your-openai-key-here",
    "your-key-here",
    "replace_me",
}


def _get_api_key() -> tuple[str, str, str]:
    """Return the configured API key, env var, and provider."""
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if provider == "gemini":
        for key_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GPT_API_KEY"):
            if os.environ.get(key_name):
                return os.environ[key_name].strip(), key_name, "gemini"
    if provider == "openai":
        for key_name in ("GPT_API_KEY", "OPENAI_API_KEY"):
            if os.environ.get(key_name):
                return os.environ[key_name].strip(), key_name, "openai"

    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip(), "GEMINI_API_KEY", "gemini"
    if os.environ.get("GOOGLE_API_KEY"):
        return os.environ["GOOGLE_API_KEY"].strip(), "GOOGLE_API_KEY", "gemini"
    if os.environ.get("GPT_API_KEY"):
        return os.environ["GPT_API_KEY"].strip(), "GPT_API_KEY", "openai"
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"].strip(), "OPENAI_API_KEY", "openai"
    return "", "GEMINI_API_KEY", "gemini"


def _is_placeholder_key(api_key: str) -> bool:
    normalized = api_key.strip().strip('"').strip("'").lower()
    return (
        not normalized
        or normalized.startswith("sk-...")
        or normalized.startswith("your-")
        or normalized.startswith("your_")
        or "your_api" in normalized
        or "your-openai-key" in normalized
        or "placeholder" in normalized
        or normalized in PLACEHOLDER_KEYS
        or normalized in {"changeme", "none", "null"}
    )


def _completion_with_gemini(api_key: str, conversation_history: list) -> str:
    model = os.environ.get("GEMINI_MODEL", os.environ.get("GPT_MODEL", "gemini-2.5-flash"))
    endpoint = os.environ.get(
        "GEMINI_ENDPOINT",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

    contents = []
    for message in conversation_history:
        role = "model" if message.get("role") == "assistant" else "user"
        content = str(message.get("content", "")).strip()
        if content:
            contents.append({"role": role, "parts": [{"text": content}]})

    payload = json.dumps({
        "system_instruction": {"parts": [{"text": KOKO_SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 1024,
            "temperature": 0.65,
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(part.get("text", "") for part in parts).strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching Gemini endpoint: {e.reason}") from e
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Unexpected Gemini response format: {e}") from e


def _completion_with_openai(api_key: str, key_source: str, conversation_history: list) -> str:
    endpoint = os.environ.get("GPT_ENDPOINT", "https://api.openai.com/v1/chat/completions")
    model = os.environ.get("GPT_MODEL", "gpt-4o")

    messages = [{"role": "system", "content": KOKO_SYSTEM_PROMPT}] + conversation_history

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.65,
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "X-Koko-Key-Source": key_source,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GPT API error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching GPT endpoint: {e.reason}") from e
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Unexpected GPT response format: {e}") from e


def get_completion(conversation_history: list) -> str:
    """
    Send a conversation to the configured GPT endpoint and return the reply text.

    Args:
        conversation_history: List of message dicts, e.g.:
            [{"role": "user", "content": "Who has a denied case?"}]
            The system prompt is automatically prepended.

    Returns:
        The model's reply as a plain string.

    Raises:
        RuntimeError: If the API call fails or credentials are missing.
    """
    api_key, key_source, provider = _get_api_key()

    if _is_placeholder_key(api_key):
        raise RuntimeError(
            "Koko needs a real API key before it can answer. "
            "Set GEMINI_API_KEY, GPT_API_KEY, or OPENAI_API_KEY in .env, then restart python3 server.py."
        )

    if provider == "gemini":
        return _completion_with_gemini(api_key, conversation_history)
    return _completion_with_openai(api_key, key_source, conversation_history)

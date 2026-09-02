"""
Pluggable LLM provider layer.

Supported providers (LLM_PROVIDER env var):
  gemini   — Google Generative Language REST API (default)
  mistral  — La Plateforme, OpenAI-compatible
  groq     — Groq Cloud, OpenAI-compatible (free tier, no vision/embed)

Public API — callers never change regardless of provider:
  generate(messages, *, json_mode, temperature) -> str
  generate_json(messages, *, temperature)       -> dict
  stream(messages, *, temperature)              -> Iterator[str]
  ocr_images(images, *, mime_type, max_tokens)  -> List[str]
  ocr_image(image_bytes, *, mime_type)          -> str
  embed(texts, *, dim, task_type)               -> List[List[float]]   (Gemini)
  embed_mistral(texts)                          -> List[List[float]]   (Mistral)

LLMError(message, *, retry_after=None) is the shared error type.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Iterator, List, Union

import httpx

from .config import settings

log = logging.getLogger("docusense.llm")

# ── Gemini base URL ──────────────────────────────────────────────────────────
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# ── OpenAI-compatible base URLs ───────────────────────────────────────────────
_MISTRAL_BASE = "https://api.mistral.ai/v1"
_GROQ_BASE    = "https://api.groq.com/openai/v1"

Messages = Union[str, List[dict]]


# ────────────────────────────────────────────────────────────────────────────
# Shared error type
# ────────────────────────────────────────────────────────────────────────────

class LLMError(RuntimeError):
    """Raised when the LLM backend is unavailable or returns an error.

    `retry_after` carries the server-suggested wait (seconds) for a 429 so
    callers can auto-retry after the free-tier bucket refills; None otherwise.
    """
    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# ────────────────────────────────────────────────────────────────────────────
# Provider dispatch
# ────────────────────────────────────────────────────────────────────────────

def _provider() -> str:
    return settings.LLM_PROVIDER.lower().strip()


# ────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────────────

_OCR_PAGE_DELIM = "<<<PAGE-BREAK>>>"


def _to_prompt(messages: Messages) -> str:
    """Flatten a message list into a single prompt string."""
    if isinstance(messages, str):
        return messages
    parts = []
    for m in messages:
        role = str(m.get("role", "user")).upper()
        parts.append(f"{role}: {m.get('content', '')}")
    return "\n\n".join(parts)


def _to_oai_messages(messages: Messages) -> List[dict]:
    """Convert to OpenAI-style message list."""
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    return [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]


def _loads_loose(raw: str) -> dict:
    """Parse JSON that may be wrapped in ```json ... ``` fences or have prose around it."""
    if not raw:
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def _ocr_prompt(n: int) -> str:
    if n == 1:
        return (
            "Transcribe ALL text in this document page image exactly as it appears. "
            "Preserve reading order, line breaks, headings and lists. Render any tables "
            "as rows with cells separated by ' | '. Output plain text only — no "
            "commentary, no translation, no markdown code fences. If the page has no "
            "legible text, reply with nothing."
        )
    return (
        f"You are given {n} document page images, in order. Transcribe ALL text from "
        f"EACH page exactly as it appears (preserve reading order, line breaks, headings, "
        f"lists; render tables as rows of cells separated by ' | '). Output the {n} "
        f"transcriptions in order, separated by a line containing ONLY '{_OCR_PAGE_DELIM}'. "
        f"Emit exactly {n - 1} separator lines so there are {n} sections; use an empty "
        f"section for a blank page. Output plain text only — no commentary, no markdown "
        f"code fences."
    )


# ────────────────────────────────────────────────────────────────────────────
# Gemini provider
# ────────────────────────────────────────────────────────────────────────────

def _gemini_require_key() -> None:
    if not settings.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY is not configured. Set it in your environment.")


def _gemini_payload(prompt: str, *, json_mode: bool = False, temperature: float = 0.2) -> dict:
    gen_cfg: dict = {"temperature": temperature, "maxOutputTokens": settings.LLM_MAX_OUTPUT_TOKENS}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    return {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen_cfg}


def _gemini_extract_text(data: dict) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        return ""


def _parse_retry_delay(data: dict, default: float = 30.0) -> float:
    try:
        for d in (data.get("error", {}).get("details") or []):
            if str(d.get("@type", "")).endswith("RetryInfo"):
                rd = str(d.get("retryDelay", "")).rstrip("s")
                if rd:
                    return min(float(rd) + 1.0, 60.0)
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        msg = data.get("error", {}).get("message", "") if isinstance(data, dict) else ""
        m = re.search(r"retry in ([\d.]+)s", msg)
        if m:
            return min(float(m.group(1)) + 1.0, 60.0)
    except (ValueError, TypeError, AttributeError):
        pass
    return default


def _gemini_error(status_code: int, data: dict) -> str:
    err = data.get("error", {}) if isinstance(data, dict) else {}
    msg = err.get("message", f"HTTP {status_code}")
    status = err.get("status", "")
    if status_code == 429 or status == "RESOURCE_EXHAUSTED" or "quota" in msg.lower():
        return ("Gemini free-tier quota exceeded — wait ~30–60s and retry, or switch to "
                f"another provider. (details: {msg})")
    return f"LLM request failed: {msg}"


def _gemini_generate(messages: Messages, *, json_mode: bool = False, temperature: float = 0.2) -> str:
    _gemini_require_key()
    prompt = _to_prompt(messages)
    url = f"{_GEMINI_BASE}/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
    try:
        with httpx.Client(timeout=90) as client:
            resp = client.post(url, json=_gemini_payload(prompt, json_mode=json_mode, temperature=temperature))
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach Gemini: {e}") from e
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "error" in data:
        raise LLMError(_gemini_error(resp.status_code, data))
    text = _gemini_extract_text(data).strip()
    if not text:
        cands = data.get("candidates") or []
        reason = cands[0].get("finishReason", "") if cands else ""
        block = (data.get("promptFeedback") or {}).get("blockReason", "")
        detail = block or reason or "no content returned"
        hint = " — raise LLM_MAX_OUTPUT_TOKENS" if reason == "MAX_TOKENS" else ""
        raise LLMError(f"The model returned no text (reason: {detail}){hint}.")
    return text


def _gemini_stream(messages: Messages, *, temperature: float = 0.2) -> Iterator[str]:
    _gemini_require_key()
    prompt = _to_prompt(messages)
    url = (f"{_GEMINI_BASE}/{settings.GEMINI_MODEL}:streamGenerateContent"
           f"?alt=sse&key={settings.GEMINI_API_KEY}")
    payload = _gemini_payload(prompt, temperature=temperature)
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                    retry_after = _parse_retry_delay(data) if resp.status_code == 429 else None
                    raise LLMError(_gemini_error(resp.status_code, data), retry_after=retry_after)
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    text = _gemini_extract_text(obj)
                    if text:
                        yield text
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach Gemini: {e}") from e


def _gemini_ocr_images(images: List[bytes], *, mime_type: str = "image/jpeg",
                       max_tokens: int | None = None) -> List[str]:
    _gemini_require_key()
    if not images:
        return []
    n = len(images)
    parts: List[dict] = [{"text": _ocr_prompt(n)}]
    for img in images:
        parts.append({"inline_data": {"mime_type": mime_type, "data": base64.b64encode(img).decode("ascii")}})
    url = f"{_GEMINI_BASE}/{settings.GEMINI_MODEL}:generateContent?key={settings.GEMINI_API_KEY}"
    payload = {"contents": [{"role": "user", "parts": parts}],
               "generationConfig": {"temperature": 0.0, "maxOutputTokens": max_tokens or settings.LLM_MAX_OUTPUT_TOKENS}}
    with httpx.Client(timeout=180) as client:
        for attempt in range(3):
            try:
                resp = client.post(url, json=payload)
            except httpx.HTTPError as e:
                raise LLMError(f"Could not reach the OCR backend: {e}") from e
            data = resp.json() if resp.content else {}
            if resp.status_code == 429 and attempt < 2:
                wait = _parse_retry_delay(data)
                log.warning("OCR rate-limited; waiting %.0fs then retrying (%d page(s))", wait, n)
                time.sleep(wait)
                continue
            if resp.status_code >= 400 or "error" in data:
                raise LLMError(_gemini_error(resp.status_code, data))
            text = _gemini_extract_text(data)
            if n == 1:
                return [text.strip()]
            sections = [s.strip() for s in text.split(_OCR_PAGE_DELIM)]
            if len(sections) != n:
                raise ValueError(f"OCR batch returned {len(sections)} sections for {n} pages")
            return sections
    raise LLMError("OCR failed after repeated rate-limit retries.")


# ────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible helper (shared by Mistral and Groq)
# ────────────────────────────────────────────────────────────────────────────

def _oai_retry_after(resp: httpx.Response) -> float:
    """Parse Retry-After header (seconds or HTTP-date) from a 429."""
    ra = resp.headers.get("retry-after", "")
    try:
        return min(float(ra) + 1.0, 60.0)
    except ValueError:
        pass
    return 30.0


def _oai_generate(base_url: str, api_key: str, model: str, messages: Messages,
                  *, json_mode: bool = False, temperature: float = 0.2) -> str:
    body: dict = {
        "model": model,
        "messages": _to_oai_messages(messages),
        "temperature": temperature,
        "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=90) as client:
            resp = client.post(f"{base_url}/chat/completions", json=body, headers=headers)
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach LLM backend: {e}") from e
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        msg = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
        retry_after = _oai_retry_after(resp) if resp.status_code == 429 else None
        raise LLMError(f"LLM request failed: {msg}", retry_after=retry_after)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        text = ""
    if not text:
        raise LLMError("The model returned no text. Please try again.")
    return text.strip()


def _oai_stream(base_url: str, api_key: str, model: str,
                messages: Messages, *, temperature: float = 0.2) -> Iterator[str]:
    body = {
        "model": model,
        "messages": _to_oai_messages(messages),
        "temperature": temperature,
        "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", f"{base_url}/chat/completions", json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                    msg = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
                    retry_after = _oai_retry_after(resp) if resp.status_code == 429 else None
                    raise LLMError(f"LLM request failed: {msg}", retry_after=retry_after)
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        obj = json.loads(chunk)
                        delta = obj["choices"][0]["delta"].get("content") or ""
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach LLM backend: {e}") from e


# ────────────────────────────────────────────────────────────────────────────
# Mistral-specific: OCR (Pixtral) and embeddings
# ────────────────────────────────────────────────────────────────────────────

def _mistral_require_key() -> None:
    if not settings.MISTRAL_API_KEY:
        raise LLMError("MISTRAL_API_KEY is not configured. Set it in your environment.")


def _mistral_ocr_images(images: List[bytes], *, mime_type: str = "image/jpeg",
                        max_tokens: int | None = None) -> List[str]:
    _mistral_require_key()
    if not images:
        return []
    n = len(images)
    content: List[dict] = [{"type": "text", "text": _ocr_prompt(n)}]
    for img in images:
        b64 = base64.b64encode(img).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
    body = {
        "model": settings.MISTRAL_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "max_tokens": max_tokens or settings.LLM_MAX_OUTPUT_TOKENS,
    }
    headers = {"Authorization": f"Bearer {settings.MISTRAL_API_KEY}", "Content-Type": "application/json"}
    with httpx.Client(timeout=180) as client:
        for attempt in range(3):
            try:
                resp = client.post(f"{_MISTRAL_BASE}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise LLMError(f"Could not reach Mistral OCR: {e}") from e
            if resp.status_code == 429 and attempt < 2:
                wait = _oai_retry_after(resp)
                log.warning("Mistral OCR rate-limited; waiting %.0fs (%d pages)", wait, n)
                time.sleep(wait)
                continue
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                msg = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
                raise LLMError(f"Mistral OCR failed: {msg}")
            text = data["choices"][0]["message"]["content"] or ""
            if n == 1:
                return [text.strip()]
            sections = [s.strip() for s in text.split(_OCR_PAGE_DELIM)]
            if len(sections) != n:
                raise ValueError(f"OCR batch returned {len(sections)} sections for {n} pages")
            return sections
    raise LLMError("Mistral OCR failed after repeated rate-limit retries.")


def embed_mistral(texts: List[str]) -> List[List[float]]:
    """Embed texts using Mistral's embedding model (1024-dim)."""
    _mistral_require_key()
    if not texts:
        return []
    headers = {"Authorization": f"Bearer {settings.MISTRAL_API_KEY}", "Content-Type": "application/json"}
    out: List[List[float]] = []
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(texts), settings.EMBED_BATCH):
            group = texts[i:i + settings.EMBED_BATCH]
            body = {"model": settings.MISTRAL_EMBED_MODEL, "inputs": [t or " " for t in group]}
            try:
                resp = client.post(f"{_MISTRAL_BASE}/embeddings", json=body, headers=headers)
            except httpx.HTTPError as e:
                raise LLMError(f"Could not reach Mistral embeddings: {e}") from e
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                msg = (data.get("error") or {}).get("message", f"HTTP {resp.status_code}")
                raise LLMError(f"Mistral embedding failed: {msg}")
            out.extend(item["embedding"] for item in data.get("data", []))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Public API — callers use these; never call provider internals directly
# ────────────────────────────────────────────────────────────────────────────

def generate(messages: Messages, *, json_mode: bool = False, temperature: float = 0.2) -> str:
    """Blocking generation — returns the full response text."""
    p = _provider()
    if p == "mistral":
        return _oai_generate(_MISTRAL_BASE, settings.MISTRAL_API_KEY,
                             settings.MISTRAL_MODEL, messages,
                             json_mode=json_mode, temperature=temperature)
    if p == "groq":
        if not settings.GROQ_API_KEY:
            raise LLMError("GROQ_API_KEY is not configured. Set it in your environment.")
        return _oai_generate(_GROQ_BASE, settings.GROQ_API_KEY,
                             settings.GROQ_MODEL, messages,
                             json_mode=json_mode, temperature=temperature)
    # default: gemini
    return _gemini_generate(messages, json_mode=json_mode, temperature=temperature)


def generate_json(messages: Messages, *, temperature: float = 0.1) -> dict:
    """Generate and parse a JSON object. Retries in plain-text mode if strict JSON fails."""
    try:
        data = _loads_loose(generate(messages, json_mode=True, temperature=temperature))
        if data:
            return data
    except LLMError:
        pass
    data = _loads_loose(generate(messages, json_mode=False, temperature=temperature))
    if not data:
        raise LLMError("The model did not return usable JSON. Please try again in a moment.")
    return data


def stream(messages: Messages, *, temperature: float = 0.2) -> Iterator[str]:
    """Stream generation as text deltas."""
    p = _provider()
    if p == "mistral":
        yield from _oai_stream(_MISTRAL_BASE, settings.MISTRAL_API_KEY,
                               settings.MISTRAL_MODEL, messages, temperature=temperature)
        return
    if p == "groq":
        if not settings.GROQ_API_KEY:
            raise LLMError("GROQ_API_KEY is not configured. Set it in your environment.")
        yield from _oai_stream(_GROQ_BASE, settings.GROQ_API_KEY,
                               settings.GROQ_MODEL, messages, temperature=temperature)
        return
    yield from _gemini_stream(messages, temperature=temperature)


def ocr_images(images: List[bytes], *, mime_type: str = "image/jpeg",
               max_tokens: int | None = None) -> List[str]:
    """
    Transcribe one or more page images in a single multimodal request.

    Groq has no vision model — for scanned PDFs, set OCR_BACKEND=tesseract
    in your env when using Groq. The RAG pipeline will fall back automatically.
    """
    p = _provider()
    if p == "mistral":
        return _mistral_ocr_images(images, mime_type=mime_type, max_tokens=max_tokens)
    if p == "groq":
        raise LLMError("Groq does not support vision/OCR. Set OCR_BACKEND=tesseract in your env.")
    return _gemini_ocr_images(images, mime_type=mime_type, max_tokens=max_tokens)


def ocr_image(image_bytes: bytes, *, mime_type: str = "image/jpeg",
              max_tokens: int | None = None) -> str:
    """Transcribe a single image — convenience wrapper over ocr_images()."""
    out = ocr_images([image_bytes], mime_type=mime_type, max_tokens=max_tokens)
    return out[0] if out else ""


def embed(texts: List[str], *, dim: int | None = None,
          task_type: str = "RETRIEVAL_DOCUMENT") -> List[List[float]]:
    """Batch-embed texts with the Gemini embedding API."""
    _gemini_require_key()
    if not texts:
        return []
    dim = dim or settings.GEMINI_EMBED_DIM
    model = settings.GEMINI_EMBED_MODEL
    model_path = model if model.startswith("models/") else f"models/{model}"
    url = f"{_GEMINI_BASE}/{model}:batchEmbedContents?key={settings.GEMINI_API_KEY}"
    out: List[List[float]] = []
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(texts), settings.EMBED_BATCH):
            group = texts[i:i + settings.EMBED_BATCH]
            body = {"requests": [
                {"model": model_path,
                 "content": {"parts": [{"text": (t or " ")[:20000]}]},
                 "taskType": task_type,
                 "outputDimensionality": dim}
                for t in group
            ]}
            try:
                resp = client.post(url, json=body)
            except httpx.HTTPError as e:
                raise LLMError(f"Could not reach the embedding backend: {e}") from e
            data = resp.json() if resp.content else {}
            if resp.status_code >= 400 or "error" in data:
                raise LLMError(_gemini_error(resp.status_code, data))
            embs = data.get("embeddings", [])
            if len(embs) != len(group):
                raise LLMError(f"Embedding count mismatch: got {len(embs)}, expected {len(group)}")
            out.extend(e.get("values", []) for e in embs)
    return out

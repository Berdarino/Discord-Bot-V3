"""Client for a local Ollama server.

Ollama exposes a plain HTTP API on localhost, so this is another thin aiohttp
client like ``klipy.py`` or ``mal.py`` — no SDK and no new dependency.

Running the model locally rather than calling a hosted API changes three things
this client has to account for:

* **It is slow, and the first call is much slower.** Ollama unloads a model
  after five minutes idle; reloading 8B of weights off disk can take half a
  minute. Hence the long timeout and ``KEEP_ALIVE``.
* **It serves one generation at a time.** Concurrency does not help — it just
  makes everyone wait. The cog serialises calls rather than letting a busy
  channel pile requests onto the GPU.
* **Qwen3 thinks out loud by default.** ``think: false`` turns that off, but it
  has been unreliable across Qwen3 builds, so leaked blocks are stripped too.
  Without that, the bot posts its own reasoning into the channel as its reply.

Docs: https://docs.ollama.com — request shape verified against the published
API reference, not against a live server: Ollama was not installed here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

_log = logging.getLogger(__name__)

# Generous: a cold start pays for reading the weights off disk, and an 8B model
# on consumer hardware is an order of magnitude slower than a hosted API.
_TIMEOUT = aiohttp.ClientTimeout(total=180)

# How long Ollama keeps the model resident after a reply. The default is 5
# minutes, which for a chat bot means most messages pay the reload cost.
KEEP_ALIVE = "30m"

# Qwen3 wraps reasoning in these. `think: false` should prevent them; stripping
# is the belt to that braces, because a leaked block would be posted verbatim.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# A truncated reply can carry a closing tag with no opening one.
_ORPHAN_CLOSE = re.compile(r"^.*?</think>", re.DOTALL | re.IGNORECASE)


class OllamaError(RuntimeError):
    """Ollama was unreachable, or refused the request."""


class OllamaModelMissingError(OllamaError):
    """The server is up but does not have the configured model pulled."""


class OllamaClient:
    """One local model, asked for one reply at a time."""

    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._session: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return self._model

    async def _get_session(self) -> aiohttp.ClientSession:
        """Create the session lazily; aiohttp binds it to the running loop."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Ask for one reply. Raises ``OllamaError`` if it cannot be produced."""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            # Discord cannot stream text, and the reply is short, so there is
            # nothing to gain from incremental output.
            "think": False,
            "keep_alive": KEEP_ALIVE,
            "options": {
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }

        session = await self._get_session()
        try:
            async with session.post(f"{self._base_url}/api/chat", json=payload) as r:
                status = r.status
                try:
                    body = await r.json(content_type=None)
                except ValueError as exc:
                    raise OllamaError(f"Ollama sent a non-JSON response (HTTP {status}).") from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise OllamaError("Could not reach Ollama. Is `ollama serve` running?") from exc

        if status == 404:
            raise OllamaModelMissingError(
                f"Ollama does not have {self._model!r}. Run: ollama pull {self._model}"
            )
        if status != 200 or not isinstance(body, dict):
            raise OllamaError(f"Ollama returned HTTP {status}: {_error_text(body)}")

        content = ((body.get("message") or {}).get("content")) or ""
        reply = strip_thinking(str(content))
        if not reply:
            raise OllamaError("Ollama returned an empty reply.")
        return reply


def strip_thinking(text: str) -> str:
    """Remove any reasoning the model emitted despite ``think: false``.

    Qwen3 marks reasoning with ``<think>`` tags. A reply cut short by
    ``num_predict`` can also end up with a closing tag and no opening one, which
    is why the orphan case is handled separately rather than by one pattern.
    """
    cleaned = _THINK_BLOCK.sub("", text)
    if "</think>" in cleaned:
        cleaned = _ORPHAN_CLOSE.sub("", cleaned)
    # An unclosed opening tag means everything after it is reasoning.
    if "<think>" in cleaned:
        cleaned = cleaned.split("<think>", 1)[0]
    return cleaned.strip()


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("error") or body)[:200]
    return str(body)[:200]

"""RIDE.LOG narrator: casual lines and an acrostic poem from the story facts (ADR-0021).

One Messages API call (structured output) with the facts JSON and the rules; it never computes
a number or picks a place (ADR-0004). The caller validates the answer
(:func:`cyp.reports.ride_story.validate`) and falls back to the deterministic text. A refusal,
a truncated answer or malformed JSON returns ``None``; an API failure raises
:class:`NarrationError`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from cyp.core.errors import CypError

MODEL = "claude-opus-5-5"
MAX_TOKENS = 1024
TIMEOUT_S = 30.0
MAX_RETRIES = 2
FALLBACK_BETA = "server-side-fallback-2026-07-01"

PROMPT_PATH = Path(__file__).resolve().parent / "ride_log_prompt.txt"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {"type": "string"}},
        "poem": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["lines", "poem"],
    "additionalProperties": False,
}


@cache
def system_prompt() -> str:
    """The rules (``ride_log_prompt.txt``)."""
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


class NarrationError(CypError):
    """The Messages API call failed (network, auth, rate limit, server)."""


@dataclass(frozen=True)
class Narration:
    """The narrator's answer, not yet validated."""

    lines: list[str]
    poem: list[str]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class Narrator(Protocol):
    """``(facts, head) -> Narration | None``; tests inject fakes."""

    def __call__(
        self, facts: dict[str, Any], head: str, problems: Sequence[str] = ()
    ) -> Narration | None:
        """Lines and poem for ``facts`` with the acrostic ``head``.

        ``problems``: the rules the previous answer broke (a retry).
        """
        ...  # pragma: no cover


def user_message(facts: dict[str, Any], head: str, problems: Sequence[str] = ()) -> str:
    """The one user turn: the head, the facts JSON (keys sorted) and, on a retry, the problems."""
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    text = f"head: {head}\nfacts: {payload}"
    if problems:
        listed = "\n".join(f"- {p}" for p in problems)
        text += (
            f"\nYour previous answer broke these rules; write a new one that keeps them:\n{listed}"
        )
    return text


def parse(text: str) -> tuple[list[str], list[str]] | None:
    """``(lines, poem)`` from the JSON answer (``None`` when it is not the schema)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    lines, poem = data.get("lines"), data.get("poem")
    if not isinstance(lines, list) or not isinstance(poem, list):
        return None
    if not all(isinstance(x, str) for x in [*lines, *poem]):
        return None
    return [x.strip() for x in lines], [x.strip().lstrip(">").strip() for x in poem]


@dataclass
class ClaudeNarrator:
    """The real narrator (``anthropic`` SDK); ``client`` is injectable for tests."""

    api_key: str
    model: str = MODEL
    client: Any = None

    def _client(self) -> Any:
        if self.client is None:
            import anthropic

            self.client = anthropic.Anthropic(
                api_key=self.api_key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES
            )
        return self.client

    def __call__(
        self, facts: dict[str, Any], head: str, problems: Sequence[str] = ()
    ) -> Narration | None:
        """One call; ``None`` on a refusal, a cut-off answer or JSON outside the schema.

        Raises:
            NarrationError: the API call failed.
        """
        import anthropic

        try:
            response = self._client().beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system_prompt(),
                messages=[{"role": "user", "content": user_message(facts, head, problems)}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIError as exc:
            raise NarrationError(f"{type(exc).__name__}: {exc}") from exc
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        parsed = parse(text)
        if parsed is None:
            return None
        usage = getattr(response, "usage", None)
        return Narration(
            lines=parsed[0],
            poem=parsed[1],
            model=str(getattr(response, "model", self.model)),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

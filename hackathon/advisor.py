"""hackathon/advisor.py -- an LLM veto gate for the hackathon agent, ported
from the production pattern in `advisors/__init__.py` (read there, not
imported: this directory ships to the public contest repo and must not carry
our code -- see the module docstring in hackathon/live.py).

DESIGN, and it is the same one production uses:

  THE MODEL CAN ONLY VETO, NEVER AUTHORIZE. The deterministic gates in
  `hackathon/executor.py` already decided the trade is worth opening before
  `review()` is ever called (it is wired as the LAST gate in
  `hackathon/live.py`, after the six deterministic checks). If the model
  says "fine, go ahead", nothing changes -- the order was already going to be
  sent. If it raises a clear objection, the cycle ends in "nada" and the
  objection becomes the recorded reason.

  IT FAILS OPEN. No `FEATHERLESS_API_KEY`, a network error, a timeout, or an
  answer that cannot be parsed all mean `veto=False` -- the trade proceeds --
  and the returned `reason` says exactly which of those happened. An advisor
  that takes the agent down when it is unavailable is worse than no advisor:
  it would turn a single point of failure (one AI provider's uptime) into a
  reason the whole strategy stops trading.

MODEL AND ENDPOINT -- verified against Featherless's own docs on 2026-08-29
(https://featherless.ai/docs/quickstart-guide and
https://featherless.ai/docs/completions), not invented:

  - Base URL: https://api.featherless.ai/v1 (OpenAI-compatible).
  - Endpoint: POST https://api.featherless.ai/v1/chat/completions.
  - Auth: `Authorization: Bearer <FEATHERLESS_API_KEY>` header.
  - Default model: `Qwen/Qwen2.5-7B-Instruct` -- it is the example id used in
    Featherless's own quickstart snippet, so it is confirmed reachable through
    this API rather than guessed; it is a small, fast instruct model, which
    fits a task that is one short yes/no judgment per candidate, not
    open-ended reasoning. Both the base URL and the model are overridable via
    `FEATHERLESS_BASE_URL` / `FEATHERLESS_MODEL` in case Featherless retires
    this id or a better-suited open model becomes available -- this file does
    not hardcode a bet on either staying valid forever.

Contract: review(candidate, context=None, ask=None, timeout=20.0) -> dict
    {"veto": bool, "reason": str, "consulted": bool, "model": str | None}
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

DEFAULT_BASE_URL = "https://api.featherless.ai/v1"
DEFAULT_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_TIMEOUT = 20.0

_SYSTEM_PROMPT = (
    "You are a skeptical risk reviewer for a PAPER-trading options agent that "
    "sells cash-secured puts. A deterministic screen has already approved this "
    "candidate on capital, concentration, spread, liquidity, market hours and "
    "idempotency -- do not re-check any of that. Your only job is to flag a "
    "CLEAR, SPECIFIC reason this particular contract should NOT be sold right "
    "now (e.g. an imminent binary event on the underlying, a delta/DTE that "
    "looks stale or wrong for the stated strategy, a bid/ask that looks broken "
    "despite passing the spread ratio check). If you are unsure, or the "
    "objection is vague, DO NOT veto -- vetoing is for clear cases only. "
    "Reply with ONLY a JSON object, no other text: "
    '{"veto": true|false, "reason": "one short sentence"}'
)


def _default_ask(prompt: str, *, api_key: str, base_url: str, model: str,
                  timeout: float) -> str:
    """Call Featherless's OpenAI-compatible chat completions endpoint.

    Plain `requests` on purpose: `requests` is already a project dependency
    (requirements.txt) and the `openai` client is not installed anywhere in
    this repo -- adding it just for one call would be a new dependency for a
    single POST to an OpenAI-shaped API.
    """
    import requests

    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 150,
            "temperature": 0,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def _build_prompt(candidate: dict, context: Optional[dict]) -> str:
    lines = [
        "Proposed trade: SELL 1 cash-secured PUT.",
        f"- Underlying: {candidate.get('underlying')}",
        f"- Symbol: {candidate.get('symbol')}",
        f"- Strike: {candidate.get('strike')}",
        f"- Expiry: {candidate.get('expiry')}",
        f"- Delta: {candidate.get('delta')}",
        f"- Bid/Ask: {candidate.get('bid')}/{candidate.get('ask')}",
        f"- Volume: {candidate.get('volume')}",
        f"- Open interest: {candidate.get('open_interest')}",
    ]
    if context:
        lines.append("Additional context:")
        for key, value in context.items():
            lines.append(f"  - {key}: {value}")
    lines.append(
        "Is there any clear, specific reason NOT to sell this put right now?")
    return "\n".join(lines)


def _parse_verdict(raw: str) -> dict:
    """Extract {"veto": bool, "reason": str} from the model's raw text.

    Tolerant of the model wrapping the JSON in prose or a code fence: it
    looks for the first '{' and the last '}' rather than requiring the whole
    response to be pure JSON, the same tolerance production's advisor uses.
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in the model's answer")
    payload = json.loads(raw[start:end + 1])
    if "veto" not in payload:
        raise ValueError("answer JSON has no 'veto' key")
    return {"veto": bool(payload["veto"]),
             "reason": str(payload.get("reason", "")).strip()[:300] or "no reason given"}


def review(candidate: dict, *, context: Optional[dict] = None,
           ask: Optional[Callable[[str], str]] = None,
           timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Ask an LLM whether there is a clear reason to veto `candidate`.

    Returns {"veto": bool, "reason": str, "consulted": bool, "model": str|None}.
    Never raises: every failure mode (missing key, network error, unparsable
    answer) is caught here and turned into veto=False with an explanatory
    reason -- see the module docstring for why that is the design, not an
    oversight.
    """
    api_key = os.environ.get("FEATHERLESS_API_KEY")
    if not api_key:
        return {"veto": False, "consulted": False, "model": None,
                "reason": "no FEATHERLESS_API_KEY: advisor skipped (fail-open)"}

    model = os.environ.get("FEATHERLESS_MODEL", DEFAULT_MODEL)
    base_url = os.environ.get("FEATHERLESS_BASE_URL", DEFAULT_BASE_URL)

    if ask is None:
        def ask(prompt: str) -> str:  # noqa: F811 -- intentional shadow, default impl
            return _default_ask(prompt, api_key=api_key, base_url=base_url,
                                 model=model, timeout=timeout)

    prompt = _build_prompt(candidate, context)

    try:
        raw = ask(prompt)
    except Exception as exc:  # noqa: BLE001 -- fail open by design
        return {"veto": False, "consulted": False, "model": model,
                "reason": f"advisor unreachable ({type(exc).__name__}: {exc}) -- fail-open"}

    try:
        verdict = _parse_verdict(raw)
    except Exception:  # noqa: BLE001 -- the model answered, we just can't use it
        return {"veto": False, "consulted": True, "model": model,
                "reason": f"advisor answer could not be parsed: {raw[:200]!r} -- fail-open"}

    return {"veto": verdict["veto"], "consulted": True, "model": model,
            "reason": verdict["reason"]}

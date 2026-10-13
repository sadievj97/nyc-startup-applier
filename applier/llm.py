"""Shared Claude API plumbing: prices, usage and cost logging, the daily budget, and JSON-only calls.

Rule 7: calls that read untrusted text (postings, web pages) get no tools here, must answer in a fixed
JSON schema, and the code validates the answer. Nothing a model returns can trigger a submission.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from applier.config import Config
from applier.safety import budget_status

# US dollars per million tokens (input, output), checked 2026-10-12. Web search: $10 per 1,000 searches.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
WEB_SEARCH_USD = 0.01
# Models that accept the server-side refusal fallback in its "default" form.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}


class BudgetReached(Exception):
    pass


class ModelAnswerError(Exception):
    """The model declined, ran out of room, or answered outside the required format."""


def fallback_kwargs(model: str) -> dict:
    return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}


def cost(model: str, usage: Any) -> tuple[float, dict]:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5-5"])
    tokens = {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
        "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    server = getattr(usage, "server_tool_use", None)
    tokens["searches"] = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    usd = (tokens["input"] * price_in + tokens["output"] * price_out
           + tokens["cache_write"] * price_in * 1.25 + tokens["cache_read"] * price_in * 0.1) / 1_000_000
    return usd + tokens["searches"] * WEB_SEARCH_USD, tokens


def log_usage(conn: sqlite3.Connection, model: str, purpose: str, response: Any, run_id: int | None = None) -> float:
    usd, tokens = cost(model, response.usage)
    with conn:
        conn.execute(
            """INSERT INTO api_usage (run_id, model, purpose, input_tokens, output_tokens, cache_read_tokens,
               cache_write_tokens, web_searches, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, getattr(response, "model", model), purpose, tokens["input"], tokens["output"],
             tokens["cache_read"], tokens["cache_write"], tokens["searches"], round(usd, 6)),
        )
    return usd


def require_budget(conn: sqlite3.Connection, config: Config) -> None:
    if not budget_status(conn, config).ok:
        raise BudgetReached("daily Claude API budget reached")


def json_call(client: Any, conn: sqlite3.Connection, config: Config, *, model: str, purpose: str,
              system: str | list, prompt: str, schema: dict, effort: str = "medium", max_tokens: int = 16000,
              run_id: int | None = None) -> tuple[dict, float]:
    """A request with no tools whose answer must match `schema`. Returns (answer, cost in USD)."""
    require_budget(conn, config)
    response = client.beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        **fallback_kwargs(model),
    )
    usd = log_usage(conn, model, purpose, response, run_id)
    if response.stop_reason == "refusal":
        raise ModelAnswerError("the request was declined")
    if response.stop_reason == "max_tokens":
        raise ModelAnswerError("the answer was cut off (max_tokens)")
    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return json.loads(text), usd
    except json.JSONDecodeError as exc:
        raise ModelAnswerError(f"the answer was not valid JSON ({exc})") from exc


def strict_object(properties: dict[str, dict]) -> dict:
    """A JSON-schema object where every property is required and nothing else is allowed."""
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

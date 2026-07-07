"""Shared helpers for Claude API generation modules."""

import json
import os

from anthropic import Anthropic


def get_client() -> Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export it or add it to a .env file before generating."
        )
    return Anthropic(api_key=api_key)


def unwrap_stringified_field(key: str, value):
    """Unwrap a field the model emitted as a JSON-encoded string instead of a
    native value. Observed in testing: the model sometimes re-encodes the
    *entire* {key: value} object as that field's string value, so a single
    json.loads() leaves it double-wrapped — unwrap repeatedly until it stops
    matching that self-referential shape.

    Plain string fields (e.g. an enum value like "correct", or free-text
    feedback) are not valid JSON and must be left untouched — only unwrap
    when the string actually parses as JSON matching that wrapper shape."""
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            break
        if isinstance(parsed, dict) and key in parsed:
            value = parsed[key]
        elif isinstance(parsed, list):
            value = parsed
        else:
            break
    return value

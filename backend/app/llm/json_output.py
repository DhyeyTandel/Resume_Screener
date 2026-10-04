"""Fence-stripping + validating JSON extraction (Spec 6.3)."""
from __future__ import annotations

import json
import re

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)
STRICT_SUFFIX = "STRICT: Return ONLY the JSON object. No other text."


def extract_json(raw: str) -> dict:
    m = _FENCE.match(raw or "")
    body = m.group(1) if m else (raw or "")
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    obj = json.loads(body[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("model output is not a JSON object")
    return obj

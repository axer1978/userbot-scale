"""Config from natural language: the operator describes a change, the LLM
proposes it as JSON, the schema validates it, and a human decides.

`propose()` never writes anything. It returns the proposal, the full client
overrides it would produce, whether they validate, and the leaf-by-leaf
changes against the current config. Applying it is the ordinary config save
(tenants.TenantStore.save_config), made by the operator from the panel, so
it goes through the same validation and gets the same audit row as a save
typed by hand.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Optional

import httpx

import ai_responder
import tenant_config
from tenant_config import ConfigError

SYSTEM_PROMPT = (
    "You turn an operator's request into a change to one business's chatbot "
    "configuration. You are given the JSON schema of the configuration, the "
    "current effective configuration and the business's current overrides. "
    "Answer with ONE JSON object and nothing else: the fields to change, nested "
    "exactly like the schema, containing only what the request asks for. To add "
    'to a list without replacing it, use {"append": [...]} as its value. Use only '
    "fields that exist in the schema. If the request cannot be expressed with "
    "these fields, answer {}."
)


def extract_json(text: str) -> Any:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("The model did not answer with a JSON object.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError(f"The model's JSON did not parse: {exc}") from None


def merge_patch(overrides: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """The client overrides with `patch` applied on top: sub-sections merge,
    anything else (values, lists, append directives) replaces."""
    out = copy.deepcopy(overrides)
    for key, value in patch.items():
        current = out.get(key)
        is_section = isinstance(value, dict) and "append" not in value
        if is_section and isinstance(current, dict) and "append" not in current:
            out[key] = merge_patch(current, value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def evaluate(
    patch: Any, *, industry_config: dict[str, Any], client_overrides: dict[str, Any],
) -> dict[str, Any]:
    """What applying `patch` would do. Pure, so the validation half is
    testable without a model."""
    if not isinstance(patch, dict):
        return {"proposal": patch, "overrides": client_overrides, "valid": False,
                "errors": [{"path": "(root)", "message": "the proposal is not a JSON object"}], "changes": []}
    before = tenant_config.resolve(industry_config, client_overrides).as_dict()
    merged = merge_patch(client_overrides, patch)
    try:
        after = tenant_config.resolve(industry_config, merged).as_dict()
    except ConfigError as exc:
        return {"proposal": patch, "overrides": merged, "valid": False, "errors": exc.errors, "changes": []}
    return {"proposal": patch, "overrides": merged, "valid": True, "errors": [],
            "changes": tenant_config.diff(before, after)}


async def propose(
    *,
    api_key: str,
    intent: str,
    industry_config: dict[str, Any],
    client_overrides: dict[str, Any],
    ai_config: Optional[dict[str, Any]] = None,
    client: Optional[httpx.AsyncClient] = None,
    usage_sink: Optional[ai_responder.UsageSink] = None,
) -> dict[str, Any]:
    intent = (intent or "").strip()
    if not intent:
        raise ValueError("Describe the change you want.")
    effective = tenant_config.resolve(industry_config, client_overrides).as_dict()
    user = (
        "SCHEMA:\n" + json.dumps(tenant_config.TenantConfig.model_json_schema(), separators=(",", ":"))
        + "\n\nCURRENT EFFECTIVE CONFIG:\n" + json.dumps(effective, ensure_ascii=False)
        + "\n\nCURRENT OVERRIDES FOR THIS BUSINESS:\n" + json.dumps(client_overrides, ensure_ascii=False)
        + "\n\nREQUEST:\n" + intent
    )
    text = await ai_responder._complete(
        api_key=api_key,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        ai_config={**(ai_config or {}), "model": (ai_config or {}).get("model", "deepseek-chat"),
                   "temperature": 0.0, "max_tokens": 800},
        client=client,
        **({"usage_sink": usage_sink} if usage_sink is not None else {}),
    )
    try:
        patch = extract_json(text)
    except ValueError as exc:
        return {"intent": intent, "proposal": None, "overrides": client_overrides, "valid": False,
                "errors": [{"path": "(root)", "message": str(exc)}], "changes": [], "raw": text}
    return {"intent": intent, **evaluate(patch, industry_config=industry_config, client_overrides=client_overrides)}

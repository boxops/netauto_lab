"""
Structured output helpers for pipeline stage parsing.

Uses LangChain's with_structured_output() so the LLM is forced to return
a JSON object matching the schema rather than free-form text with embedded
KEY: value pairs. Falls back to regex extraction (_parse_tail) when the
underlying LLM (e.g. Ollama) does not support function calling / tool use.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound=BaseModel)


def _parse_tail_fallback(text: str, model_class: type[_T]) -> _T:
    """
    Regex fallback for LLMs that don't support structured output.
    Extracts KEY: value pairs from anywhere in the response text and
    attempts to construct the Pydantic model from the result.
    """
    fields = model_class.model_fields
    keys = {k.upper(): k for k in fields}
    result: dict = {}
    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n:
        m = re.match(r"^([A-Z][A-Z_]+):\s*(.*)$", lines[i].strip())
        if m and m.group(1) in keys:
            key   = keys[m.group(1)]
            value = m.group(2).strip()
            if not value:
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and lines[j].strip().startswith("```"):
                    j += 1
                    code: list[str] = []
                    while j < n and not lines[j].strip().startswith("```"):
                        code.append(lines[j])
                        j += 1
                    value = "\n".join(code).strip()
                    i = j
            if value:
                result[key] = value
        i += 1

    field_info = model_class.model_fields
    for field_name, field in field_info.items():
        if field_name not in result and field.default is not None:
            result.setdefault(field_name, field.default)

    return model_class(**result)


def parse_structured(
    llm,
    prompt: str,
    model_class: type[_T],
    session_config: dict | None = None,
) -> tuple[_T, str, bool]:
    """
    Invoke the LLM and parse the response into model_class.

    Strategy:
    1. Try with_structured_output (JSON schema enforcement via API).
    2. Fall back to free-text invocation + regex extraction if structured
       output is unsupported (Ollama, older models).

    Returns (parsed_model, raw_text, parse_failed).
    parse_failed=True means all parsing strategies failed and model defaults
    were used — callers should emit a warning event so operators can see it.
    """
    from langchain_core.messages import HumanMessage

    # ── attempt structured output ──────────────────────────────────────────
    try:
        structured_llm = llm.with_structured_output(model_class)
        config = session_config or {}
        result = structured_llm.invoke(
            [HumanMessage(content=prompt)],
            config=config,
        )
        if isinstance(result, model_class):
            raw = result.model_dump_json()
            return result, raw, False
    except (NotImplementedError, AttributeError, Exception) as exc:
        logger.debug(
            "with_structured_output not available for this LLM (%s), "
            "falling back to regex extraction", type(exc).__name__
        )

    # ── fallback: free-form text + regex ──────────────────────────────────
    from langchain_core.messages import AIMessage
    config = session_config or {}
    raw_result = llm.invoke([HumanMessage(content=prompt)], config=config)
    raw_text = raw_result.content if isinstance(raw_result, AIMessage) else str(raw_result)

    try:
        parsed = _parse_tail_fallback(raw_text, model_class)
        return parsed, raw_text, False
    except (ValidationError, Exception) as exc:
        logger.warning(
            "Structured output fallback parsing failed for %s: %s — "
            "using model defaults", model_class.__name__, exc
        )
        defaults = {
            k: v.default if v.default is not None else ""
            for k, v in model_class.model_fields.items()
        }
        return model_class(**defaults), raw_text, True

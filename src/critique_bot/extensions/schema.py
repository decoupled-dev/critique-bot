"""A JSON Schema subset: enough to check a tool's arguments before anything runs.

Supported: ``type`` (object, string, integer, number, boolean, array, null, or a list of
them), ``properties``, ``required``, ``additionalProperties`` (bool), ``enum``, ``const``,
``minimum``/``maximum``, ``minLength``/``maxLength``, ``pattern``, ``items``,
``minItems``/``maxItems``, ``default`` (filled in), ``description``. Other keywords are
ignored, so a richer schema from an MCP server still validates its basic shape.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["SchemaError", "check_schema", "validate", "with_defaults"]

_TYPES = {"object", "string", "integer", "number", "boolean", "array", "null"}


class SchemaError(ValueError):
    """The schema itself is malformed."""


def check_schema(schema: Any, where: str = "parameters") -> None:
    """Raise :class:`SchemaError` when ``schema`` is not a usable object schema."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{where} must be a table/object")
    kind = schema.get("type", "object")
    kinds = kind if isinstance(kind, list) else [kind]
    for item in kinds:
        if item not in _TYPES:
            raise SchemaError(f"{where}: unknown type {item!r}")
    props = schema.get("properties", {})
    if not isinstance(props, dict):
        raise SchemaError(f"{where}.properties must be a table/object")
    for name, sub in props.items():
        check_schema(sub, f"{where}.properties.{name}")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise SchemaError(f"{where}.required must be a list of names")
    missing = [name for name in required if props and name not in props]
    if missing:
        raise SchemaError(f"{where}.required names unknown properties: {', '.join(missing)}")
    if "items" in schema:
        check_schema(schema["items"], f"{where}.items")
    if "pattern" in schema:
        try:
            re.compile(str(schema["pattern"]))
        except re.error as exc:
            raise SchemaError(f"{where}.pattern: {exc}") from exc


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "null":
        return value is None
    return True


def coerce(schema: dict[str, Any], value: Any) -> Any:
    """Loose model output made exact: "12" for an integer, "true" for a boolean."""
    kind = schema.get("type")
    if isinstance(value, str):
        text = value.strip()
        if kind == "integer" and re.fullmatch(r"-?\d+", text):
            return int(text)
        if kind == "number" and re.fullmatch(r"-?\d+(\.\d+)?", text):
            return float(text) if "." in text else int(text)
        if kind == "boolean" and text.lower() in {"true", "false"}:
            return text.lower() == "true"
    return value


def with_defaults(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """``args`` with coerced values and each missing property's ``default`` filled in."""
    out = dict(args)
    for name, sub in (schema.get("properties") or {}).items():
        if not isinstance(sub, dict):
            continue
        if name in out:
            out[name] = coerce(sub, out[name])
        elif "default" in sub:
            out[name] = sub["default"]
    return out


def validate(schema: dict[str, Any], value: Any, path: str = "arguments") -> list[str]:
    """Problems with ``value`` against ``schema`` (empty when it fits)."""
    problems: list[str] = []
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_type_ok(item, value) for item in kinds):
            return [f"{path} must be {' or '.join(kinds)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path} must be {schema['const']!r}")
    if "enum" in schema and value not in (schema.get("enum") or []):
        problems.append(f"{path} must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path} must be at most {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            problems.append(f"{path} must have at least {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            problems.append(f"{path} must have at most {schema['maxLength']} characters")
        if "pattern" in schema:
            try:
                if not re.search(str(schema["pattern"]), value):
                    problems.append(f"{path} must match {schema['pattern']}")
            except re.error:
                pass
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            problems.append(f"{path} needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            problems.append(f"{path} takes at most {schema['maxItems']} items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problems.extend(validate(items, item, f"{path}[{index}]"))
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                problems.append(f"{path}.{name} is required")
        for name, item in value.items():
            sub = props.get(name)
            if isinstance(sub, dict):
                problems.extend(validate(sub, item, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}.{name} is not a known argument (known: {', '.join(sorted(props)) or 'none'})")
    return problems

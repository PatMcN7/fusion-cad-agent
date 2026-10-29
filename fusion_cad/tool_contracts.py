"""Explicit public contracts and dependency-free validation before native work."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
from fusion_cad.errors import ArgumentError


def validate(value, schema, path="arguments"):
    kind = schema.get("type")
    valid = {"object": lambda: isinstance(value, dict),
             "array": lambda: isinstance(value, list),
             "string": lambda: isinstance(value, str),
             "boolean": lambda: isinstance(value, bool),
             "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             "integer": lambda: isinstance(value, int) and not isinstance(value, bool)}
    def fail(message, example=None):
        raise ArgumentError(f"{path}: {message}", path=path, expected=schema,
                            received_type=type(value).__name__, example=example)
    if kind not in valid or not valid[kind]():
        fail(f"expected {kind}, received {type(value).__name__}",
             ["Exact entity name"] if kind == "array" else None)
    if "enum" in schema and value not in schema["enum"]:
        fail(f"must be one of {schema['enum']}")
    if kind in ("number", "integer"):
        for key, ok in (("minimum", lambda n: value >= n), ("maximum", lambda n: value <= n)):
            if key in schema and not ok(schema[key]): fail(f"{key} is {schema[key]}")
    if kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            fail("array length is outside the declared bounds")
        for i, entry in enumerate(value): validate(entry, schema.get("items", {}), f"{path}[{i}]")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value: fail(f"missing required field {key}")
        properties = schema.get("properties", {})
        for key, entry in value.items():
            if key in properties: validate(entry, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False: fail(f"unknown field {key}")


def check_contracts(contracts):
    """Reject broken declarations before they replace the live contract file."""
    def check(schema, path):
        if not isinstance(schema, dict) or schema.get("type") not in {
                "object", "array", "string", "boolean", "number", "integer"}:
            raise ValueError(f"{path} needs an explicit supported type")
        if schema["type"] == "array": check(schema.get("items"), path + ".items")
        if schema["type"] == "object":
            properties = schema.get("properties", {})
            if not isinstance(properties, dict): raise ValueError(f"{path}.properties must be an object")
            required = schema.get("required", [])
            if not isinstance(required, list) or any(not isinstance(k, str) or k not in properties for k in required):
                raise ValueError(f"{path}.required must name declared properties")
            for key, child in properties.items(): check(child, path + "." + key)
        for key in ("minimum", "maximum", "minItems", "maxItems"):
            if key in schema and (isinstance(schema[key], bool) or not isinstance(schema[key], (int, float)) or not math.isfinite(schema[key])):
                raise ValueError(f"{path}.{key} must be a finite number")
    if not isinstance(contracts, dict) or not contracts:
        raise ValueError("contracts must be a nonempty object")
    for name, schema in contracts.items():
        check(schema, name)
        if schema["type"] != "object": raise ValueError(f"{name} arguments must be an object")


def apply(registry):
    contracts = json.loads(Path(__file__).with_name("tool_schemas.json").read_text())
    check_contracts(contracts)
    for name, spec in registry.items():
        if name in contracts:
            spec["input_schema"] = contracts[name]
        elif "input_schema" not in spec:
            args = spec.get("arguments", {})
            if any(not isinstance(v, dict) or "type" not in v for v in args.values()):
                raise ValueError(f"{name} needs an explicit input_schema; English descriptions are not types")
            spec["input_schema"] = {"type": "object", "properties": copy.deepcopy(args),
                                    "required": spec.get("required", []), "additionalProperties": False}


def version(registry):
    return hashlib.sha256(json.dumps(registry, sort_keys=True).encode()).hexdigest()[:16]

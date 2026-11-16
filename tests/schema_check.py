"""A validator for the JSON Schema subset that schema/env.schema.json uses (Python standard library only).

Supported keywords: type, const, enum, required, properties, additionalProperties (false), pattern, minimum, maximum,
items, minItems, minLength, minProperties, anyOf, $ref (local "#/$defs/..."); annotations ($schema, $id, title,
description) are ignored. Any other keyword is an error, so the schema cannot silently use one this check skips.
"""
import json
import os
import re

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "schema", "env.schema.json")
ANNOTATIONS = {"$schema", "$id", "title", "description"}
TYPES = {"object": dict, "array": list, "string": str, "null": type(None)}


def load_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def _type_ok(value, name):
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, TYPES[name])


def errors(value, schema, root=None, path="$"):
    """Every violation of `schema` by `value`, as 'path: message' strings (empty when valid)."""
    root = root if root is not None else schema
    out = []
    for key, rule in schema.items():
        if key in ANNOTATIONS or key == "$defs":
            continue
        if key == "$ref":
            target = root
            for part in rule.removeprefix("#/").split("/"):
                target = target[part]
            out += errors(value, target, root, path)
        elif key == "type":
            if not _type_ok(value, rule):
                out.append(f"{path}: expected {rule}")
                return out
        elif key == "const":
            if value != rule or type(value) is not type(rule):
                out.append(f"{path}: expected {rule!r}")
        elif key == "enum":
            if value not in rule:
                out.append(f"{path}: {value!r} not in {rule}")
        elif key == "anyOf":
            if all(errors(value, sub, root, path) for sub in rule):
                out.append(f"{path}: matches none of anyOf")
        elif key == "pattern":
            if isinstance(value, str) and not re.search(rule, value):
                out.append(f"{path}: {value!r} does not match {rule}")
        elif key == "minLength":
            if isinstance(value, str) and len(value) < rule:
                out.append(f"{path}: shorter than {rule}")
        elif key in ("minimum", "maximum"):
            if isinstance(value, int) and (value < rule if key == "minimum" else value > rule):
                out.append(f"{path}: {value} violates {key} {rule}")
        elif key == "required":
            if isinstance(value, dict):
                out += [f"{path}: missing {k}" for k in rule if k not in value]
        elif key == "properties":
            if isinstance(value, dict):
                for k, sub in rule.items():
                    if k in value:
                        out += errors(value[k], sub, root, f"{path}.{k}")
        elif key == "additionalProperties":
            if rule is not False:
                raise ValueError(f"unsupported additionalProperties {rule!r}")
            if isinstance(value, dict):
                out += [f"{path}: unexpected {k}" for k in value if k not in schema.get("properties", {})]
        elif key == "minProperties":
            if isinstance(value, dict) and len(value) < rule:
                out.append(f"{path}: fewer than {rule} members")
        elif key == "items":
            if isinstance(value, list):
                for i, item in enumerate(value):
                    out += errors(item, rule, root, f"{path}[{i}]")
        elif key == "minItems":
            if isinstance(value, list) and len(value) < rule:
                out.append(f"{path}: fewer than {rule} items")
        else:
            raise ValueError(f"unsupported schema keyword {key!r} at {path}")
    return out

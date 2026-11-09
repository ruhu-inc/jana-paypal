"""AUT-07: a self-contained form schema; canonical cross-document refs retain their original meaning."""

from __future__ import annotations

import copy
import hashlib
from functools import lru_cache
from typing import Any
from urllib.parse import urldefrag, urljoin

from agent_core.contracts import document, schema_ids
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SINGLE = {"items", "additionalProperties", "contains", "not", "if", "then", "else", "propertyNames"}
MAPPING = {"properties", "patternProperties", "dependentSchemas"}
ARRAY = {"allOf", "oneOf", "anyOf", "prefixItems"}


@lru_cache
def _bundle(name: str) -> dict[str, Any]:
    root = document(name)
    registry = Registry().with_resources(
        [
            (identity, Resource.from_contents(document(identity), default_specification=DRAFT202012))
            for identity in schema_ids()
        ]
    )
    definitions: dict[str, Any] = {}
    seen: dict[str, str] = {}

    def visit(node: Any, base: str) -> Any:
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        scope = urljoin(base, node.get("$id", ""))
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("$id", "$schema", "$defs"):
                continue
            if key == "$ref":
                absolute = urljoin(scope, value)
                if absolute not in seen:
                    label = absolute.rsplit("/", 1)[-1].replace("#", "_")
                    alias = label + "__" + hashlib.sha256(absolute.encode()).hexdigest()[:16]
                    seen[absolute] = alias
                    definitions[alias] = {}
                    resolved = registry.resolver(scope).lookup(value)
                    definitions[alias] = visit(resolved.contents, urldefrag(absolute)[0])
                result[key] = "#/$defs/" + seen[absolute]
            elif key in SINGLE:
                result[key] = visit(value, scope)
            elif key in MAPPING:
                result[key] = {field: visit(child, scope) for field, child in value.items()}
            elif key in ARRAY:
                result[key] = [visit(child, scope) for child in value]
            else:
                result[key] = copy.deepcopy(value)
        return result

    result: dict[str, Any] = visit(root, root["$id"])
    for key, value in root.get("$defs", {}).items():
        definitions[key] = visit(value, root["$id"])
    result["$defs"] = definitions
    return result


def form_schema(name: str) -> dict[str, Any]:
    return copy.deepcopy(_bundle(name))

"""Optional draft curriculum relations; authored data, never model or learner supplied code."""

import ast
import functools
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Relation:
    quantities: dict[str, tuple[str, ...]]
    left: str
    right: str
    exclude: tuple[str, ...]


@functools.lru_cache(maxsize=128)
def parse(body: str) -> Relation | None:
    blocks = re.findall(r"(?m)^```jana-relations\s*\n([\s\S]*?)^```\s*$", body)
    if not blocks:
        if re.search(r"(?m)^```jana-relations\b", body):
            raise ValueError("curriculum relation block must be closed")
        return None
    if len(blocks) != 1 or len(blocks[0]) > 4096:
        raise ValueError("exactly one bounded curriculum relation block is allowed")
    data = json.loads(blocks[0])
    if not isinstance(data, dict) or set(data) != {"quantities", "left", "right", "exclude"}:
        raise ValueError("relation needs quantities, left, right and exclude")
    quantities = data["quantities"]
    if not isinstance(quantities, dict) or not 2 <= len(quantities) <= 8:
        raise ValueError("relation needs two to eight named quantities")
    aliases: set[str] = set()
    for name, terms in quantities.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z_]{0,31}", name)
                or not isinstance(terms, list) or not 1 <= len(terms) <= 8):
            raise ValueError("invalid relation quantity or aliases")
        for term in terms:
            if not isinstance(term, str) or not re.fullmatch(r"[a-z][a-z '-]{0,59}", term) or term in aliases:
                raise ValueError("aliases must be bounded, unique literal phrases")
            aliases.add(term)
    used: list[str] = []
    for side in (data["left"], data["right"]):
        if not isinstance(side, str) or len(side) > 160:
            raise ValueError("relation sides must be bounded arithmetic")
        tree = ast.parse(side, mode="eval")
        nodes = list(ast.walk(tree))
        if len(nodes) > 40 or any(not isinstance(n, (ast.Expression, ast.BinOp, ast.Name, ast.Load,
                                                    ast.Add, ast.Sub, ast.Mult, ast.Div)) for n in nodes):
            raise ValueError("relations contain only named quantities and arithmetic operators")
        if any(isinstance(n, ast.Name) and n.id not in quantities for n in nodes):
            raise ValueError("relation references an undeclared quantity")
        used += [n.id for n in nodes if isinstance(n, ast.Name)]
    if set(used) != set(quantities) or len(used) != len(quantities):
        raise ValueError("each relation quantity must occur exactly once")
    excluded = data["exclude"]
    if not isinstance(excluded, list) or len(excluded) > 32 or any(
        not isinstance(t, str) or not re.fullmatch(r"[a-z][a-z -]{0,39}", t) for t in excluded
    ):
        raise ValueError("invalid excluded scope terms")
    return Relation({k: tuple(v) for k, v in quantities.items()}, data["left"], data["right"], tuple(excluded))

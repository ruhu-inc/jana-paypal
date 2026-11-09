"""Drawings a tool returns for the client to draw (VSL-06, VSL-23). chem_organic (VER-33) draws each structure as SVG;
before a resolver sees it, the engine sanitises it as library SVG is, normalises it, and checks that every part it
draws is one of the tool's atom or bond ids. The assets layer cannot reach the SVG rules in the store (ARC-18), so
the check sits here, on the engine's tool call."""

from __future__ import annotations

from typing import Any

from agent_core.assets.resolvers import Call, ResolutionError
from agent_core.store.svg import normalise, part_ids, sanitise_problems

ORGANIC_SVG_MAX_BYTES = 100_000  # chem_organic's drawing, at most (science-compute OrganicResult)


def checked(call: Call) -> Call:
    """`call` with chem_organic's drawings sanitised and normalised, or refused."""

    def run(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = call(tool, args)
        value = result.get("value")
        if tool != "chem_organic" or not isinstance(value, dict):
            return result
        structures = []
        for st in value.get("structures") or []:
            problems = sanitise_problems(st["svg"], ORGANIC_SVG_MAX_BYTES)
            svg = normalise(st["svg"]) if not problems else ""
            drawn = set(part_ids(svg)) if svg else set()
            if problems or not drawn <= set(st["atom_ids"]) | set(st["bond_ids"]):
                raise ResolutionError(f"chem_organic's drawing of {st.get('canonical_smiles', '?')} is not one the "
                                      "client may draw")  # fmt: skip
            structures.append({**st, "svg": svg})
        return {**result, "value": {**value, "structures": structures}}

    return run

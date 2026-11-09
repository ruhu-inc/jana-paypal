"""The organic and A level chemistry kinds in the engine (VSL-06, VSL-07, VSL-19, VSL-41, VSL-46): organic_structure,
reaction_mechanism, energy_levels, orbital_boxes, maxwell_boltzmann, born_haber_cycle, concentration_time and
spectrum_chart.

For each kind: the part ids (derived from the params' own level, step, species and peak ids, never from where they are
drawn), the claim numbers a student can read in the params (VSL-12), the build layers in the renderer's drawing order
(VSL-41), and the alternative text (VSL-14). Orbital boxes and spectra are pack data (N3) resolved here without a
tool: an electron configuration from the pack's proton number by the filling order and Hund's rule below, and a
spectrum from an authored pack asset (content schema 1.6 `science_assets`), never predicted from a molecule. The
kinds that call worker tools resolve in `resolvers.py`."""

from __future__ import annotations

import re
from collections.abc import Callable
from fractions import Fraction
from typing import Any

from agent_core.assets import visual_kinds
from agent_core.assets.visual_kinds import Part, Parts, View, _join, say

# --- part ids (VSL-19, VSL-46) ---------------------------------------------------------------------------------------


def part_id(raw: str) -> str:
    """A params id as a part id segment: the characters VSL-19 allows, others as underscores."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(raw))


def unique_part_ids(ids: list[str], what: str) -> dict[str, str]:
    """Each id's part id; an id that repeats, as written or once made a part id, is refused (VSL-46)."""
    out: dict[str, str] = {}
    for raw in ids:
        made = part_id(raw)
        if raw in out or made in out.values():
            raise ValueError(f"{what} id {raw} repeats")
        out[raw] = made
    return out


# --- claim numbers (VSL-12) ------------------------------------------------------------------------------------------

CLAIM_PATHS: dict[str, list[str]] = {
    "energy_levels": ["levels.*.energy_ev"],
    "maxwell_boltzmann": ["temperatures_k.*", "activation_energy_j_mol"],
    "born_haber_cycle": ["steps.*.enthalpy_kj_mol"],
    # Plateaus and the event time of the qualitative mode are relative and illustrative, not claims.
    "concentration_time": [
        "species.*.initial",
        "species.*.rate_constant",
        "species.*.points.*.time",
        "species.*.points.*.concentration",
    ],
}

# --- orbital boxes (N3) ----------------------------------------------------------------------------------------------

# The filling order (Madelung) as far as krypton, the boxes per subshell, and the two first-row d-block atoms whose
# ground state moves a 4s electron into 3d (a half-filled or filled 3d).
FILLING = ("1s", "2s", "2p", "3s", "3p", "4s", "3d", "4p")
BOXES = {"s": 1, "p": 3, "d": 5}
D_BLOCK_EXCEPTIONS = {24: {"3d": 5, "4s": 1}, 29: {"3d": 10, "4s": 1}}
MAX_ELECTRONS = 36


def _fill(electrons: int) -> dict[str, int]:
    counts, left = {}, electrons
    for sub in FILLING:
        counts[sub] = min(left, 2 * BOXES[sub[-1]])
        left -= counts[sub]
    return counts


def _order(sub: str) -> tuple[int, int]:
    return int(sub[0]), "spdf".index(sub[1])


def configuration(z: int, charge: int) -> dict[str, int]:
    """Electrons per subshell for an atom or ion: the neutral atom fills in order (with the Cr and Cu exceptions); a
    cation loses electrons from the highest shell first (4p, then 4s, then 3d), an anion fills on in order."""
    electrons = z - charge
    if not 1 <= z <= MAX_ELECTRONS or not 0 < electrons <= MAX_ELECTRONS:
        raise ValueError(f"{electrons} electrons is outside the orbital boxes drawn (1 to {MAX_ELECTRONS})")
    if charge < 0:
        return _fill(electrons)
    counts = _fill(z)
    counts.update(D_BLOCK_EXCEPTIONS.get(z, {}))
    lose = charge
    for sub in sorted(counts, key=_order, reverse=True):
        taken = min(lose, counts[sub])
        counts[sub] -= taken
        lose -= taken
    return counts


def boxes(sub: str, electrons: int) -> list[list[str]]:
    """Hund's rule: one electron in each box with parallel spins before any box takes a pair."""
    n = BOXES[sub[-1]]
    out: list[list[str]] = [[] for _ in range(n)]
    for k in range(electrons):
        out[k % n].append("up" if k < n else "down")
    return out


def orbital_boxes(params: dict[str, Any], pack_value: Callable[[str], tuple[str, Fraction]]) -> dict[str, Any]:
    """The configuration from the pack's proton number (chem.z_<element>) and the charge; subshells the neutral atom
    fills are shown even when an ion has emptied them (Fe2+ keeps an empty 4s box)."""
    element, charge = str(params["element"]), int(params["charge"])
    ref, z = pack_value(f"chem.z_{element.lower()}")
    counts = configuration(int(z), charge)
    neutral = configuration(int(z), 0)
    shown = sorted((sub for sub in FILLING if counts[sub] or neutral[sub]), key=_order)
    subshells: list[dict[str, Any]] = [
        {"id": sub, "electrons": counts[sub], "boxes": boxes(sub, counts[sub])} for sub in shown
    ]
    labels = {f"sub_{sub}": f"{sub}{counts[sub]}" for sub in shown}
    labels["configuration"] = " ".join(f"{sub}{counts[sub]}" for sub in shown if counts[sub])
    unpaired = sum(1 for sub in shown for box in boxes(sub, counts[sub]) if len(box) == 1)
    return {"subshells": subshells, "unpaired": unpaired, "pack_refs": [ref], "labels": labels}


# --- spectra (N3) ----------------------------------------------------------------------------------------------------


def position(unit: str, value: float) -> str:
    """A peak position as written on a spectrum: m/z 46, 1050 cm⁻¹, 1.2 ppm."""
    return f"m/z {value:g}" if unit == "m/z" else f"{value:g} {unit.replace('^-1', '⁻¹')}"


def spectrum(params: dict[str, Any], assets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The authored spectrum the params name: its id, type and every requested peak must exist in the pack."""
    asset = assets.get(str(params["spectrum_id"]))
    if asset is None or asset.get("kind") != "spectrum":
        raise ValueError(f"no spectrum {params['spectrum_id']} in the installed packs")
    if not asset.get("licence"):
        raise ValueError(f"{asset['id']} cannot be shown yet")
    definition = asset["definition"]
    if definition["type"] != params["type"]:
        raise ValueError(f"{asset['id']} is a {definition['type']} spectrum, not {params['type']}")
    peaks = {p["id"]: p for p in definition["peaks"]}
    if len(peaks) != len(definition["peaks"]):
        raise ValueError(f"{asset['id']} repeats a peak id")
    wanted = list(params.get("peaks") or [])
    unknown = [p for p in wanted if p not in peaks]
    if unknown or len(set(wanted)) != len(wanted):
        raise ValueError(f"{asset['id']} has no peak {', '.join(unknown) or 'twice'}")
    labels = {
        f"peak_{pid}": f"{position(definition['x_unit'], peaks[pid]['position'])}, {peaks[pid]['assignment']}"
        for pid in wanted
    }
    pack_ref = f"pack:{asset.get('pack_id', 'pack.unknown')}/science_assets/{asset['id']}"
    return {
        "title": asset["title"],
        "spectrum": {k: definition[k] for k in ("type", "x_unit", "y_unit")},
        "peaks": sorted(definition["peaks"], key=lambda p: p["position"]),
        "pack_refs": [pack_ref],
        "labels": labels,
    }


# --- reaction mechanisms ---------------------------------------------------------------------------------------------


def mechanism_check(params: dict[str, Any], entry: dict[str, Any]) -> None:
    """A vetted library sequence only (VSL-06): the diagram is the ref, the sequence is one it declares and the step
    is inside it; curly arrows are the entry's own, never placed from a request."""
    if params["diagram_id"] != entry.get("id"):
        raise ValueError(f"diagram_id {params['diagram_id']} is not the ref {entry.get('id')}")
    sequence = next((s for s in entry.get("sequences", []) if s.get("id") == params["sequence_id"]), None)
    if sequence is None:
        raise ValueError(f"{entry.get('id')} has no sequence {params['sequence_id']}")
    if not 0 <= int(params["step"]) < len(sequence.get("steps", [])):
        raise ValueError(
            f"{params['sequence_id']} has {len(sequence.get('steps', []))} steps, not {params['step'] + 1}"
        )


# --- qualitative equilibrium responses (VSL-46) ----------------------------------------------------------------------


def expected_moves(params: dict[str, Any]) -> dict[str, str]:
    """How each species' plateau must move after the event: rises, falls or unchanged (Le Chatelier, for a closed
    system at fixed volume). Raises for a change the qualitative mode does not draw."""
    event = params["event"]
    change = event["change"]
    species = {sp["id"]: sp for sp in params["species"]}
    if change == "catalyst":
        return dict.fromkeys(species, "unchanged")
    if change in ("temperature_increase", "temperature_decrease"):
        enthalpy = float(event["forward_enthalpy_kj_mol"])
        if enthalpy == 0:
            raise ValueError("a temperature change needs a non-zero forward enthalpy to shift the equilibrium")
        # Raising the temperature favours the endothermic direction.
        forward = (enthalpy > 0) == (change == "temperature_increase")
        return {sid: ("rises" if (sp["role"] == "product") == forward else "falls") for sid, sp in species.items()}
    if change in ("add_species", "remove_species"):
        target = species.get(str(event.get("species_id")))
        if target is None:
            raise ValueError(f"{event.get('species_id')} is not a species on the graph")
        added = change == "add_species"
        moves = {}
        for sid, sp in species.items():
            same_side = sp["role"] == target["role"]
            if sid == target["id"]:
                moves[sid] = "rises" if added else "falls"
            else:
                moves[sid] = ("falls" if same_side else "rises") if added else ("rises" if same_side else "falls")
        return moves
    raise ValueError(f"{change.replace('_', ' ')} needs the compression ratio; it is not drawn qualitatively")


# --- parts per kind (VSL-19) -----------------------------------------------------------------------------------------


def _energy_levels(v: View) -> Parts:
    levels = v.params.get("levels", [])
    out = [
        Part(f"lvl_{part_id(lv['id'])}", v.name("energy_level", label=lv["label"]), "line", quizzable=True)
        for lv in levels
    ]
    names = {lv["id"]: lv["label"] for lv in levels}
    for tr in v.params.get("transitions", []):
        label = f"{names.get(tr['from'], tr['from'])} to {names.get(tr['to'], tr['to'])}"
        out.append(Part(f"tr_{part_id(tr['from'])}_{part_id(tr['to'])}", v.name("transition", label=label), "arrow"))
    return out


def _orbital_boxes(v: View) -> Parts:
    return [
        Part(f"sub_{s['id']}", v.name("subshell", label=s["id"]), "row", quizzable=True)
        for s in v.computed.get("subshells", [])
    ]


def _maxwell_boltzmann(v: View) -> Parts:
    temperatures = v.params.get("temperatures_k", [])
    out = [Part(f"curve_{n}", v.name("mb_curve", label=f"{t:g} K"), "curve") for n, t in enumerate(temperatures, 1)]
    out.append(Part("ea", v.name("activation_marker"), "line", quizzable=True))
    return out + [
        Part(f"tail_{n}", v.name("mb_tail", label=f"{t:g} K"), "region") for n, t in enumerate(temperatures, 1)
    ]


def _born_haber(v: View) -> Parts:
    return [
        Part(
            f"step_{part_id(st['id'])}",
            v.name("bh_step", label=say("alt", f"bh_{st['kind']}", v.locale)),
            "arrow",
            quizzable=True,
        )
        for st in v.params.get("steps", [])
    ]


def _concentration_time(v: View) -> Parts:
    params = v.params
    out = []
    for sp in params.get("species", []):
        out.append(Part(f"sp_{part_id(sp['id'])}", v.name("species_curve", label=sp["label"]), "curve", quizzable=True))
    if params.get("mode") == "first_order":
        for sp in params.get("species", []):
            pid = part_id(sp["id"])
            if f"sp_{pid}.half_life" in (v.computed.get("labels") or {}):
                out.append(Part(f"sp_{pid}.half_life", v.name("species_half_life", label=sp["label"]), "point"))
            if params.get("show_initial_tangent"):
                out.append(Part(f"sp_{pid}.tangent", v.name("initial_tangent", label=sp["label"]), "line"))
    if params.get("mode") == "qualitative_equilibrium" and params.get("event"):
        out.append(Part("event", v.name("event"), "line"))
    return out


def _spectrum(v: View) -> Parts:
    out = [Part("trace", v.name("spectrum_trace"), "curve")]
    peaks = {p["id"]: p for p in v.computed.get("peaks", [])}
    for pid in v.params.get("peaks") or []:
        if pid in peaks:
            out.append(Part(f"peak_{pid}", v.name("peak", label=peaks[pid]["assignment"]), "point", quizzable=True))
    return out


def _organic(v: View) -> Parts:
    out = []
    for n, mol in enumerate(v.computed.get("structures", []), 1):
        out.append(Part(f"mol_{n}", v.name("molecule", label=v.label(f"mol_{n}")), "structure"))
        for group in mol.get("highlighted", []):
            out.append(Part(f"mol_{n}.fg_{part_id(group)}", v.name("functional_group", label=group), "region"))
    # Atoms and bonds are addressable for steps and pointing, but are not tab stops of their own.
    for n, mol in enumerate(v.computed.get("structures", []), 1):
        out += [Part(f"mol_{n}.{a}", v.name("atom", label=a), "atom", focusable=False) for a in mol["atom_ids"]]
        out += [Part(f"mol_{n}.{b}", v.name("bond", label=b), "bond", focusable=False) for b in mol["bond_ids"]]
    return out


PARTS: dict[str, Callable[[View], Parts]] = {
    "energy_levels": _energy_levels,
    "orbital_boxes": _orbital_boxes,
    "maxwell_boltzmann": _maxwell_boltzmann,
    "born_haber_cycle": _born_haber,
    "concentration_time": _concentration_time,
    "spectrum_chart": _spectrum,
    "organic_structure": _organic,
}
# Kinds whose parts depend on what resolution computes: their step ids are checked again once resolved.
DYNAMIC_PARTS = frozenset({"orbital_boxes", "spectrum_chart", "organic_structure", "concentration_time"})


# --- build layers (VSL-41) -------------------------------------------------------------------------------------------


def _ids_with(parts: Parts, *prefixes: str) -> list[str]:
    return [p.id for p in parts if p.id.startswith(prefixes)]


def layers(view: View, parts: Parts) -> list[dict[str, Any]]:
    """The layers each renderer draws in, in order, with the parts each adds; [] for a kind that declares none."""
    kind = view.kind
    plan: list[tuple[str, list[str]]]
    if kind == "energy_levels":
        plan = [("levels", _ids_with(parts, "lvl_")), ("transitions", _ids_with(parts, "tr_"))]
    elif kind == "orbital_boxes":
        plan = [("axis", []), ("boxes", _ids_with(parts, "sub_")), ("electrons", [])]
    elif kind == "maxwell_boltzmann":
        plan = [
            ("axes", []),
            ("curves", _ids_with(parts, "curve_")),
            ("activation_energy", ["ea"]),
            ("tails", _ids_with(parts, "tail_")),
        ]
    elif kind == "born_haber_cycle":
        unknown = f"step_{part_id(str(view.params.get('unknown_step')))}"
        known = [p for p in _ids_with(parts, "step_") if p != unknown]
        plan = [("levels", []), ("steps", known), ("unknown", [unknown])]
    elif kind == "concentration_time":
        curves = [p.id for p in parts if p.id.startswith("sp_") and "." not in p.id]
        features = [p.id for p in parts if p.id not in curves]
        plan = [("axes", []), ("curves", curves), ("features", features)]
    elif kind == "spectrum_chart":
        plan = [("axes", []), ("trace", ["trace"]), ("peaks", _ids_with(parts, "peak_"))]
    else:
        return []
    return [{"layer": name, "part_ids": ids} for name, ids in plan]


# --- alternative text (VSL-14) ---------------------------------------------------------------------------------------


def _alt_energy_levels(v: View) -> str:
    def a(key: str, **kw: Any) -> str:
        return say("alt", key, v.locale, **kw)

    levels = {lv["id"]: lv for lv in v.params["levels"]}
    energies = [float(lv["energy_ev"]) for lv in levels.values()]
    lowest = min(levels.values(), key=lambda lv: float(lv["energy_ev"]))
    highest = max(levels.values(), key=lambda lv: float(lv["energy_ev"]))
    found = {t["id"]: t for t in v.computed.get("transitions", [])}
    transitions = []
    for tr in v.params["transitions"]:
        tid = f"tr_{part_id(tr['from'])}_{part_id(tr['to'])}"
        photon = v.label(tid)
        direction = found.get(tid, {}).get("direction", "emission")
        transitions.append(
            a(
                "energy_levels_transition",
                direction=a(f"direction_{direction}"),
                **{"from": levels[tr["from"]]["label"], "to": levels[tr["to"]]["label"]},
                energy=a("energy_levels_photon", energy=photon) if photon else "",
            )
        )
    return a(
        "energy_levels",
        count=len(energies),
        lowest=f"{lowest['label']} ({lowest['energy_ev']:g} eV)",
        highest=f"{highest['label']} ({highest['energy_ev']:g} eV)",
        transitions=_join(transitions, v.locale),
    )


def _alt_orbitals(v: View) -> str:
    charge = int(v.params.get("charge", 0))
    sign = "" if charge == 0 else f"{abs(charge) if abs(charge) > 1 else ''}{'+' if charge > 0 else '-'}"
    return say(
        "alt",
        "orbital_boxes",
        v.locale,
        species=f"{v.params['element']}{sign}",
        configuration=v.label("configuration"),
        unpaired=v.computed.get("unpaired", 0),
    )


def _alt_maxwell(v: View) -> str:
    temperatures = v.params["temperatures_k"]
    fractions = [
        say("alt", "maxwell_boltzmann_fraction", v.locale, fraction=v.label(f"tail_{n}"), temperature=f"{t:g}")
        for n, t in enumerate(temperatures, 1)
    ]
    return say(
        "alt",
        "maxwell_boltzmann",
        v.locale,
        temperatures=_join([f"{t:g} K" for t in temperatures], v.locale),
        ea=f"{v.params['activation_energy_j_mol']:g}",
        fractions=_join(fractions, v.locale),
    )


def _alt_born_haber(v: View) -> str:
    steps = [
        say(
            "alt",
            "born_haber_step",
            v.locale,
            kind=say("alt", f"bh_{st['kind']}", v.locale),
            value=v.label(f"step_{part_id(st['id'])}"),
        )
        for st in v.params["steps"]
    ]
    unknown = next(st for st in v.params["steps"] if st["id"] == v.params["unknown_step"])
    return say(
        "alt",
        "born_haber_cycle",
        v.locale,
        compound=v.params["compound"],
        count=len(steps),
        steps=_join(steps, v.locale),
        unknown=say("alt", f"bh_{unknown['kind']}", v.locale),
        value=v.label(f"step_{part_id(unknown['id'])}"),
    )


def _alt_concentration(v: View) -> str:
    p = v.params
    names = _join([sp["label"] for sp in p["species"]], v.locale)
    if p["mode"] == "data":
        lo, hi = p["time_range"]
        return say(
            "alt",
            "concentration_time_data",
            v.locale,
            species=names,
            unit=p["time_unit"],
            **{"from": f"{lo:g}", "to": f"{hi:g}"},
        )
    if p["mode"] == "first_order":
        halves = [
            say(
                "alt",
                "concentration_time_half_life",
                v.locale,
                species=sp["label"],
                value=v.label(f"sp_{part_id(sp['id'])}.half_life"),
            )
            for sp in p["species"]
            if v.label(f"sp_{part_id(sp['id'])}.half_life")
        ]
        return say(
            "alt",
            "concentration_time_first_order",
            v.locale,
            species=names,
            half_lives=("; " + _join(halves, v.locale)) if halves else "",
        )
    moves = v.computed.get("moves", {})
    said = [
        say("alt", f"moves_{moves[sp['id']]}", v.locale, species=sp["label"])
        for sp in p["species"]
        if sp["id"] in moves
    ]
    return say(
        "alt",
        "concentration_time_qualitative",
        v.locale,
        equation=v.computed.get("equation", ""),
        change=say("alt", f"change_{p['event']['change']}", v.locale),
        moves=_join(said, v.locale),
    )


def _alt_spectrum(v: View) -> str:
    wanted = [v.label(f"peak_{pid}") for pid in v.params.get("peaks") or []]
    labelled = say("alt", "spectrum_labelled", v.locale, peaks="; ".join(wanted)) if wanted else ""
    return say(
        "alt",
        "spectrum_chart",
        v.locale,
        type=say("alt", f"spectrum_{v.params['type']}", v.locale),
        title=v.computed.get("title", ""),
        count=len(v.computed.get("peaks", [])),
        labelled=labelled,
    )


def _alt_organic(v: View) -> str:
    mols = [
        f"{v.label(f'mol_{n}')} ({v.label(f'mol_{n}.formula')})"
        for n, _ in enumerate(v.computed.get("structures", []), 1)
    ]
    relationship = v.computed.get("relationship")
    tail = (
        say("alt", "organic_relationship", v.locale, relationship=say("alt", f"relationship_{relationship}", v.locale))
        if relationship
        else ""
    )
    return say(
        "alt",
        "organic_structure",
        v.locale,
        representation=say("alt", f"representation_{v.params['representation']}", v.locale),
        names=_join(mols, v.locale),
        relationship=tail,
    )


def _alt_mechanism(v: View) -> str:
    sequences: list[dict[str, Any]] = (v.entry or {}).get("sequences", [])
    sequence = next((s for s in sequences if s.get("id") == v.params.get("sequence_id")), {})
    return say(
        "alt",
        "reaction_mechanism",
        v.locale,
        alt=(v.entry or {}).get("alt_text", ""),
        title=sequence.get("title", ""),
        step=int(v.params.get("step", 0)) + 1,
        count=len(sequence.get("steps", [])),
    )


ALT: dict[str, Callable[[View], str]] = {
    "energy_levels": _alt_energy_levels,
    "orbital_boxes": _alt_orbitals,
    "maxwell_boltzmann": _alt_maxwell,
    "born_haber_cycle": _alt_born_haber,
    "concentration_time": _alt_concentration,
    "spectrum_chart": _alt_spectrum,
    "organic_structure": _alt_organic,
    "reaction_mechanism": _alt_mechanism,
}


visual_kinds.PARTS.update(PARTS)
visual_kinds.CLAIM_PATHS.update(CLAIM_PATHS)
visual_kinds.ALT_RULES.update(ALT)
visual_kinds.LAYERS.update(dict.fromkeys(PARTS, layers))

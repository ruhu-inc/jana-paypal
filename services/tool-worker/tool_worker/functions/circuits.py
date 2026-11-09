"""DC circuits (VER-16): up to 20 components solved exactly by linear nodal analysis.

Components are resistors, lamps (as resistors), cells and batteries (e.m.f. with optional internal resistance), ideal
ammeters (no resistance), ideal voltmeters (no current) and switches. The topology is either nested series and
parallel groups (`network`, a series loop when it holds a cell) or a netlist of terminal connections (`connections`,
terminals `a`/`b`, and `pos`/`neg` for cells). Modified nodal analysis in exact fractions gives every node's
potential and the current through every zero-resistance element; each component's current, p.d. and power follow.

A loop with no resistance has no unique solution: across an ideal cell it is a short circuit, a `DOMAIN_ERROR` with
reason `short_circuit`; through meters and switches alone, reason `zero_resistance_loop`. The equivalent resistance
is that of the external circuit seen by the cell, or by a chain of cells in series, found with a 1 A test current."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import sympy as sp

from tool_worker.functions.maths import exact
from tool_worker.functions.quantities import number
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
TERMINALS = {"cell": ("neg", "pos"), "battery": ("neg", "pos")}  # every other kind: ("a", "b")
FIELDS = {
    "resistor": {"resistance"},
    "lamp": {"resistance"},
    "cell": {"emf", "internal_resistance"},
    "battery": {"emf", "internal_resistance"},
    "switch": {"state"},
    "ammeter": set(),
    "voltmeter": set(),
}
MAX_DEPTH = 8


@dataclass
class Part:
    """A component with its two terminal nodes, in (a, b) or (neg, pos) order."""

    id: str
    kind: str
    first: int
    second: int
    resistance: Fraction = Fraction(0)
    emf: Fraction = Fraction(0)
    internal: Fraction = Fraction(0)
    closed: bool = True

    @property
    def is_cell(self) -> bool:
        return self.kind in ("cell", "battery")

    @property
    def conducts(self) -> bool:
        return self.kind != "voltmeter" and (self.kind != "switch" or self.closed)


def _fraction(raw: Any, field: str) -> Fraction:
    value = number(raw, field)
    return Fraction(int(value.p), int(value.q))


def _parts(components: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Components by id, with their values checked against their kind."""
    by_id: dict[str, dict[str, Any]] = {}
    for component in components:
        cid, kind = component["id"], component["kind"]
        if cid in by_id:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid} is listed twice")
        extra = sorted(set(component) - {"id", "kind"} - FIELDS[kind])
        if extra:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: a {kind} does not take {', '.join(extra)}")
        if kind in ("resistor", "lamp") and "resistance" not in component:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: a {kind} needs its resistance")
        if kind in ("cell", "battery") and "emf" not in component:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: a {kind} needs its emf")
        if kind == "switch" and "state" not in component:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: a switch needs its state, open or closed")
        by_id[cid] = component
    return by_id


def _make(component: dict[str, Any], first: int, second: int) -> Part:
    cid = component["id"]
    part = Part(cid, component["kind"], first, second, closed=component.get("state", "closed") == "closed")
    if "resistance" in component:
        part.resistance = _fraction(component["resistance"], f"{cid}.resistance")
        if part.resistance <= 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: resistance must be positive")
    if "emf" in component:
        part.emf = _fraction(component["emf"], f"{cid}.emf")
        if part.emf <= 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: emf must be positive")
    if "internal_resistance" in component:
        part.internal = _fraction(component["internal_resistance"], f"{cid}.internal_resistance")
        if part.internal < 0:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid}: internal resistance cannot be negative")
    return part


# --- topology -----------------------------------------------------------------------------------------------------


def _from_network(network: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> tuple[list[Part], tuple[int, int]]:
    """Nested groups to parts; a network holding a cell closes into a loop, otherwise its two ends are kept."""
    parts: list[Part] = []
    used: set[str] = set()
    nodes = [2]  # 0 and 1 are the network's ends

    def fresh() -> int:
        nodes[0] += 1
        return nodes[0] - 1

    def place(item: Any, start: int, end: int, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise WorkerError(Code.LIMIT_EXCEEDED, f"groups are nested more than {MAX_DEPTH} deep", limit="components")
        if isinstance(item, str):
            if item not in by_id:
                raise WorkerError(Code.DOMAIN_ERROR, f"{item} is not in components")
            if item in used:
                raise WorkerError(Code.DOMAIN_ERROR, f"{item} appears twice in the network")
            used.add(item)
            parts.append(_make(by_id[item], start, end))  # a cell is placed neg to pos along the series order
            return
        [(how, members)] = item.items()
        if how == "parallel":
            for member in members:
                place(member, start, end, depth + 1)
            return
        joints = [start, *(fresh() for _ in members[1:]), end]
        for i, member in enumerate(members):
            place(member, joints[i], joints[i + 1], depth + 1)

    has_cell = any(c["kind"] in ("cell", "battery") for c in by_id.values())
    if has_cell and "series" not in network:
        raise WorkerError(Code.DOMAIN_ERROR, "a network with a cell is a series loop: start it with series")
    place(network, 0, 0 if has_cell else 1, 0)
    unused = sorted(set(by_id) - used)
    if unused:
        raise WorkerError(Code.DOMAIN_ERROR, f"{', '.join(unused)} is in components but not in the network")
    return parts, (0, 1)


def _from_netlist(connections: list[dict[str, str]], by_id: dict[str, dict[str, Any]]) -> list[Part]:
    """Connected terminals share a node (union-find); a terminal connected to nothing is a node of its own."""
    parent: dict[str, str] = {}

    def find(t: str) -> str:
        parent.setdefault(t, t)
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    def terminal(text: str) -> str:
        cid, _, end = text.partition(".")
        if cid not in by_id:
            raise WorkerError(Code.DOMAIN_ERROR, f"{cid} is not in components")
        if end not in TERMINALS.get(by_id[cid]["kind"], ("a", "b")):
            raise WorkerError(Code.DOMAIN_ERROR, f"{text}: a {by_id[cid]['kind']} has no terminal {end}")
        return text

    for link in connections:
        a, b = find(terminal(link["from"])), find(terminal(link["to"]))
        parent[a] = b
    numbering: dict[str, int] = {}
    parts = []
    for cid in sorted(by_id):
        ends = [find(f"{cid}.{t}") for t in TERMINALS.get(by_id[cid]["kind"], ("a", "b"))]
        first, second = (numbering.setdefault(root, len(numbering)) for root in ends)
        parts.append(_make(by_id[cid], first, second))
    return parts


# --- analysis -----------------------------------------------------------------------------------------------------


@dataclass
class Branch:
    """One element of the nodal model: a resistance, or a source fixing V(q) - V(p) (a 0 V source for a meter or a
    closed switch) whose current from p to q is an unknown."""

    part: str
    p: int
    q: int
    resistance: Fraction | None = None
    emf: Fraction | None = None


def _branches(parts: list[Part], next_node: int) -> list[Branch]:
    branches = []
    for part in parts:
        if part.kind in ("resistor", "lamp"):
            branches.append(Branch(part.id, part.first, part.second, resistance=part.resistance))
        elif part.is_cell and part.internal:
            inner, next_node = next_node, next_node + 1  # the e.m.f. then the internal resistance
            branches.append(Branch(part.id, part.first, inner, emf=part.emf))
            branches.append(Branch(part.id, inner, part.second, resistance=part.internal))
        elif part.is_cell:
            branches.append(Branch(part.id, part.first, part.second, emf=part.emf))
        elif part.conducts:  # ammeter or closed switch
            branches.append(Branch(part.id, part.first, part.second, emf=Fraction(0)))
    return branches


def _check_loops(branches: list[Branch], kinds: dict[str, str]) -> None:
    """A loop of sources and zero-resistance elements has no unique solution (union-find over those elements)."""
    root: dict[int, int] = {}
    links: dict[int, list[tuple[int, str]]] = {}

    def find(n: int) -> int:
        root.setdefault(n, n)
        while root[n] != n:
            n = root[n]
        return n

    for branch in branches:
        if branch.emf is None:
            continue
        if find(branch.p) == find(branch.q):
            loop = [branch.part, *_path(links, branch.p, branch.q)]
            cells = sorted({p for p in loop if kinds[p] in ("cell", "battery")})
            if cells:
                raise WorkerError(Code.DOMAIN_ERROR, f"short circuit: a loop with no resistance across the ideal cell "
                                  f"{cells[0]}", reason="short_circuit")  # fmt: skip
            raise WorkerError(Code.DOMAIN_ERROR, f"a loop with no resistance through {', '.join(sorted(set(loop)))} "
                              "leaves the current undetermined", reason="zero_resistance_loop")  # fmt: skip
        root[find(branch.p)] = find(branch.q)
        links.setdefault(branch.p, []).append((branch.q, branch.part))
        links.setdefault(branch.q, []).append((branch.p, branch.part))


def _path(links: dict[int, list[tuple[int, str]]], start: int, goal: int) -> list[str]:
    """The elements on the forest path from start to goal."""
    came: dict[int, tuple[int, str] | None] = {start: None}
    queue = [start]
    while queue:
        node = queue.pop(0)
        for neighbour, part in links.get(node, []):
            if neighbour not in came:
                came[neighbour] = (node, part)
                queue.append(neighbour)
    out, step = [], came.get(goal)
    while step is not None:
        node, part = step
        out.append(part)
        step = came.get(node)
    return out


def _groups(branches: list[Branch], nodes: set[int]) -> dict[int, int]:
    """Each node's connected group, named by its smallest node, which is that group's 0 V reference."""
    root = {n: n for n in nodes}

    def find(n: int) -> int:
        while root[n] != n:
            n = root[n]
        return n

    for branch in branches:
        a, b = find(branch.p), find(branch.q)
        if a != b:
            root[max(a, b)] = min(a, b)
    return {n: find(n) for n in nodes}


def _solve(
    branches: list[Branch], nodes: set[int], inject: dict[int, Fraction]
) -> tuple[dict[int, Fraction], list[Fraction]]:
    """Node potentials (each group's smallest node at 0 V) and each source branch's current, exactly."""
    group = _groups(branches, nodes)
    free = sorted(n for n in nodes if group[n] != n)
    column = {n: i for i, n in enumerate(free)}
    sources = [b for b in branches if b.emf is not None]
    size = len(free) + len(sources)
    rows: list[dict[int, Fraction]] = []
    rhs: list[Fraction] = []
    # Kirchhoff's current law at every non-reference node: the currents leaving equal the current injected.
    kcl = {n: (dict[int, Fraction](), inject.get(n, Fraction(0))) for n in free}
    for branch in branches:
        if branch.resistance is not None:
            g = 1 / branch.resistance
            for here, there in ((branch.p, branch.q), (branch.q, branch.p)):
                if here in column:
                    row = kcl[here][0]
                    row[column[here]] = row.get(column[here], Fraction(0)) + g
                    if there in column:
                        row[column[there]] = row.get(column[there], Fraction(0)) - g
    for k, branch in enumerate(sources):
        j = len(free) + k
        for node, sign in ((branch.p, 1), (branch.q, -1)):
            if node in column:
                row = kcl[node][0]
                row[j] = row.get(j, Fraction(0)) + sign
        constraint: dict[int, Fraction] = {}
        for node, sign in ((branch.q, 1), (branch.p, -1)):
            if node in column:
                constraint[column[node]] = constraint.get(column[node], Fraction(0)) + sign
        rows.append(constraint)
        rhs.append(branch.emf or Fraction(0))
    for n in free:
        rows.append(kcl[n][0])
        rhs.append(kcl[n][1])
    solution = _eliminate(rows, rhs, size)
    potentials = {n: (solution[column[n]] if n in column else Fraction(0)) for n in nodes}
    return potentials, solution[len(free) :]


def _eliminate(rows: list[dict[int, Fraction]], rhs: list[Fraction], size: int) -> list[Fraction]:
    """Gaussian elimination on sparse rows in exact fractions."""
    rows = [{k: v for k, v in r.items() if v} for r in rows]
    pivots: dict[int, int] = {}
    for col in range(size):
        pick = next((i for i in range(len(rows)) if i not in pivots.values() and rows[i].get(col)), None)
        if pick is None:
            raise WorkerError(Code.DOMAIN_ERROR, "the circuit has no unique solution", reason="singular")
        pivots[col] = pick
        lead = rows[pick][col]
        for i in range(len(rows)):
            factor = rows[i].get(col) if i != pick else None
            if not factor:
                continue
            ratio = factor / lead
            for k, v in rows[pick].items():
                updated = rows[i].get(k, Fraction(0)) - ratio * v
                if updated:
                    rows[i][k] = updated
                else:
                    rows[i].pop(k, None)
            rhs[i] -= ratio * rhs[pick]
    return [rhs[pivots[col]] / rows[pivots[col]][col] for col in range(size)]


def _series_cells(parts: list[Part]) -> tuple[int, int] | None:
    """The end nodes of the cells when they form one chain in series (a single cell is a chain), else None."""
    cells = [p for p in parts if p.is_cell]
    if not cells:
        return None
    attached: dict[int, int] = {}
    for part in parts:
        if part.conducts:
            for node in (part.first, part.second):
                attached[node] = attached.get(node, 0) + 1
    degree: dict[int, int] = {}
    for cell in cells:
        for node in (cell.first, cell.second):
            degree[node] = degree.get(node, 0) + 1
    ends = [n for n, d in degree.items() if d == 1]
    inner = [n for n, d in degree.items() if d == 2]
    if len(ends) != 2 or len(inner) != len(cells) - 1 or any(attached[n] != 2 for n in inner):
        return None
    return ends[0], ends[1]


def _quantity(value: Fraction) -> dict[str, Any]:
    return exact(sp.Rational(value.numerator, value.denominator))


def phys_circuit(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    """Current, p.d. and power for every component and the equivalent resistance (VER-16)."""
    if ("network" in args) == ("connections" in args):
        raise WorkerError(Code.DOMAIN_ERROR, "give the circuit as either network or connections")
    by_id = _parts(args["components"])
    ends: tuple[int, int] | None = None
    if "network" in args:
        parts, network_ends = _from_network(args["network"], by_id)
        if not any(p.is_cell for p in parts):
            ends = network_ends
    else:
        parts = _from_netlist(args["connections"], by_id)
    nodes = {n for p in parts for n in (p.first, p.second)}
    branches = _branches(parts, max(nodes) + 1)
    nodes |= {n for b in branches for n in (b.p, b.q)}
    _check_loops(branches, {p.id: p.kind for p in parts})
    potentials, source_currents = _solve(branches, nodes, {})
    current_of = {b.part: i for b, i in zip([b for b in branches if b.emf is not None], source_currents, strict=True)}
    group = _groups(branches, nodes)

    results: dict[str, dict[str, Any]] = {}
    for part in parts:
        across = potentials[part.first] - potentials[part.second]  # V(a) - V(b), or V(neg) - V(pos) for a cell
        if part.kind == "voltmeter" and group[part.first] != group[part.second]:
            raise WorkerError(Code.DOMAIN_ERROR, f"{part.id} is not connected across one circuit")
        entry: dict[str, Any] = {"kind": part.kind}
        if part.kind in ("resistor", "lamp"):
            current = across / part.resistance
            entry.update(
                current=_quantity(current), pd=_quantity(across), power=_quantity(current**2 * part.resistance)
            )
        elif part.is_cell:
            current = current_of[part.id]  # out of the positive terminal
            entry.update(current=_quantity(current), pd=_quantity(-across), power=_quantity(part.emf * current))
            if part.internal:
                entry["power_internal"] = _quantity(current**2 * part.internal)
        elif part.conducts:  # ammeter or closed switch: no p.d., no power
            current = current_of[part.id]
            entry.update(current=_quantity(current), pd=_quantity(Fraction(0)), power=_quantity(Fraction(0)))
        else:  # voltmeter or open switch: no current
            entry.update(current=_quantity(Fraction(0)), pd=_quantity(across), power=_quantity(Fraction(0)))
        if part.kind == "ammeter":
            entry["reading"] = _quantity(abs(current_of[part.id]))
        elif part.kind == "voltmeter":
            entry["reading"] = _quantity(abs(across))
        results[part.id] = entry

    value: dict[str, Any] = {
        "components": results,
        "units": {"current": "A", "pd": "V", "power": "W", "resistance": "Ω"},
        "directions": "current flows from a to b (out of pos for a cell); pd is V(a) - V(b) (V(pos) - V(neg))",
    }
    chain = ends or _series_cells(parts)
    if chain is None:
        value["equivalent_resistance"] = None
        value["note"] = "the equivalent resistance is given for one cell, or cells in a single series chain"
    else:
        external = _branches([p for p in parts if not p.is_cell], max(nodes) + 1)  # the cells removed
        outer = _groups(external, nodes)
        if outer[chain[0]] != outer[chain[1]]:
            value["equivalent_resistance"] = None
            value["open_circuit"] = True
        else:
            test, _ = _solve(external, nodes, {chain[0]: Fraction(1), chain[1]: Fraction(-1)})
            value["equivalent_resistance"] = _quantity(test[chain[0]] - test[chain[1]])
            value["open_circuit"] = False
        cells = [p for p in parts if p.is_cell]
        if cells:
            value["total_current"] = _quantity(abs(current_of[cells[0].id]))
    working = []
    if value.get("equivalent_resistance"):
        working.append({"label": "equivalent resistance", "value": value["equivalent_resistance"]["text"], "unit": "Ω"})
    return value, {"method": "exact", "confidence": "proved", "working": working}

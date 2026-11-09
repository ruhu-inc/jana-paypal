"""Biology calculators (VER-35, VER-38): `bio_genetics` and `bio_calc`, on the science-compute contract.

Genetics: a cross takes two parent genotypes written as declared allele symbols, two per locus in the same locus order
for both parents ("AaBb", "IAi"). The allele declarations alone decide the loci, the gametes and the phenotypes (no
guessing from letter case or spelling): each parent splits into consecutive symbol pairs by the longest declared
symbol, loci assort independently, and a locus shows the phenotype of its dominant allele, of both codominant alleles
("A and B"), or of the recessive one when both copies are recessive. Probabilities are exact fractions summed to 1.
Chi-squared compares observed with expected counts of equal totals (degrees of freedom = categories - 1: no fitted
parameters). Hardy-Weinberg takes q² and names the equilibrium assumptions.

Calculators: magnification (lengths in one unit, dimensionless), percentage change (a zero start is refused), a rate
per stated time unit, the Lincoln-Petersen estimate (recaptures bounded by both catches) and Simpson's index of
diversity D = 1 - Σn(n-1)/N(N-1) (N at least 2). Biology facts are not checked here; they come from pack content."""

from __future__ import annotations

from fractions import Fraction
from itertools import product
from math import gcd, lcm, sqrt
from typing import Any

from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
HARDY_WEINBERG = (
    "a large population, random mating, no selection, no mutation and no migration (Hardy-Weinberg equilibrium)"
)
MICROMETRES = {"um": 1, "mm": 1000}


def _domain(message: str) -> WorkerError:
    return WorkerError(Code.DOMAIN_ERROR, message)


def _checked(value: float, unit: str, context: CallContext) -> dict[str, Any]:
    return {"value": value, "unit": unit, "check_id": context.check_id}


# --- bio_genetics -----------------------------------------------------------------------------------------------------


class Alleles:
    """The declared alleles: symbol -> (phenotype, dominance), in declaration order."""

    def __init__(self, declared: list[dict[str, str]]) -> None:
        self.order = [a["symbol"] for a in declared]
        if len(set(self.order)) != len(self.order):
            raise _domain("each allele symbol is declared once")
        self.phenotype = {a["symbol"]: a["phenotype"] for a in declared}
        self.dominance = {a["symbol"]: a["dominance"] for a in declared}

    def split(self, genotype: str, parent: int) -> list[tuple[str, str]]:
        """Consecutive allele pairs, reading the longest declared symbol at each position."""
        symbols, at = [], 0
        longest = sorted(self.order, key=len, reverse=True)
        while at < len(genotype):
            symbol = next((s for s in longest if genotype.startswith(s, at)), None)
            if symbol is None:
                raise _domain(f"parent {parent} ({genotype}) has a symbol that is not a declared allele at {at + 1}")
            symbols.append(symbol)
            at += len(symbol)
        if not symbols or len(symbols) % 2:
            raise _domain(f"parent {parent} ({genotype}) does not have two alleles for every locus")
        return [(symbols[i], symbols[i + 1]) for i in range(0, len(symbols), 2)]

    def rank(self, genotype: str) -> list[int]:
        """A genotype's sort key: its alleles' declaration positions, locus by locus."""
        return [self.order.index(symbol) for pair in self.split(genotype, 0) for symbol in pair]

    def canonical(self, pair: tuple[str, str]) -> str:
        return "".join(sorted(pair, key=self.order.index))

    def shown(self, pair: tuple[str, str]) -> str:
        """The phenotype a locus shows: dominant over recessive, both codominant alleles, else the recessive one."""
        present = sorted(set(pair), key=self.order.index)
        strong = [s for s in present if self.dominance[s] != "recessive"]
        if not strong:
            return self.phenotype[present[0]]
        names = list(dict.fromkeys(self.phenotype[s] for s in strong))  # declaration order, each phenotype once
        if len(names) > 1 and any(self.dominance[s] == "dominant" for s in strong):
            raise _domain(
                f"{''.join(present)}: two dominant alleles with different phenotypes; declare them codominant"
            )
        return " and ".join(names)


def _cross(args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    alleles = Alleles(args["alleles"])
    parents = [alleles.split(g, n) for n, g in enumerate(args["parents"], start=1)]
    if len(parents[0]) != len(parents[1]):
        raise _domain("the parents do not have the same number of loci")
    # Each gamete carries one allele of every locus; loci assort independently.
    gametes: list[dict[tuple[str, ...], Fraction]] = []
    for loci in parents:
        counts: dict[tuple[str, ...], Fraction] = {}
        for choice in product(*loci):
            counts[choice] = counts.get(choice, Fraction(0)) + Fraction(1, 2 ** len(loci))
        gametes.append(counts)
    genotypes: dict[str, Fraction] = {}
    phenotypes: dict[str, Fraction] = {}
    for (first, p1), (second, p2) in product(gametes[0].items(), gametes[1].items()):
        pairs = list(zip(first, second, strict=True))
        genotype = "".join(alleles.canonical(pair) for pair in pairs)
        phenotype = ", ".join(alleles.shown(pair) for pair in pairs)
        genotypes[genotype] = genotypes.get(genotype, Fraction(0)) + p1 * p2
        phenotypes[phenotype] = phenotypes.get(phenotype, Fraction(0)) + p1 * p2
    # Genotypes in the alleles' declaration order (AA, Aa, aa: 1:2:1); phenotypes most likely first (3:1, 9:3:3:1).
    by_genotype = sorted(genotypes.items(), key=lambda kv: alleles.rank(kv[0]))
    by_phenotype = _ordered(phenotypes)
    value = {
        "task": "cross",
        "genotypes": [{"genotype": g, "probability": float(p)} for g, p in by_genotype],
        "phenotypes": [{"phenotype": p, "probability": float(q)} for p, q in by_phenotype],
        "gametes": [["".join(g) for g in counts] for counts in gametes],
    }
    working = [
        {"label": "genotype ratio", "value": _ratio([p for _, p in by_genotype]), "check_id": context.check_id},
        {"label": "phenotype ratio", "value": _ratio([p for _, p in by_phenotype]), "check_id": context.check_id},
    ]
    return value, {"method": "exact", "confidence": "proved", "working": working}


def _ordered(table: dict[str, Fraction]) -> list[tuple[str, Fraction]]:
    """Most likely first, then by name: probabilities are exact, so the order never depends on rounding."""
    return sorted(table.items(), key=lambda kv: (-kv[1], kv[0]))


def _ratio(probabilities: list[Fraction]) -> str:
    """The smallest whole-number ratio of exact probabilities ("3:1", "1:2:1", "9:3:3:1")."""
    denominator = lcm(*(p.denominator for p in probabilities))
    whole = [int(p * denominator) for p in probabilities]
    common = gcd(*whole)
    return ":".join(str(w // common) for w in whole)


def _chi_squared(args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    observed, expected = args["observed"], args["expected"]
    if len(observed) != len(expected):
        raise _domain("observed and expected need one count per category")
    if any(e <= 0 for e in expected):
        raise _domain("every expected count must be positive")
    if abs(sum(observed) - sum(expected)) > 1e-9 * max(1.0, sum(expected)):
        raise _domain(f"observed total {sum(observed)} does not match expected total {sum(expected)}")
    statistic = sum((o - e) ** 2 / e for o, e in zip(observed, expected, strict=True))
    value = {"task": "chi_squared", "statistic": _checked(statistic, "chi-squared", context),
             "degrees_of_freedom": len(observed) - 1}  # fmt: skip
    working = [{"label": "degrees of freedom (categories - 1, no fitted parameters)",
                "value": str(len(observed) - 1), "check_id": context.check_id}]  # fmt: skip
    return value, {"method": "exact", "confidence": "proved", "working": working}


def _hardy_weinberg(args: dict[str, Any], context: CallContext) -> tuple[dict[str, Any], Extras]:
    q_squared = float(args["recessive_frequency"])
    q = sqrt(q_squared)
    p = 1 - q
    frequencies = {"p": p, "q": q, "p_squared": p * p, "two_pq": 2 * p * q, "q_squared": q_squared}
    working = [{"label": "assumes", "value": HARDY_WEINBERG, "check_id": context.check_id}]
    return {"task": "hardy_weinberg", "frequencies": frequencies}, {"method": "exact", "confidence": "proved",
                                                                    "working": working}  # fmt: skip


def bio_genetics(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    task = args["task"]
    if task == "cross":
        return _cross(args, context)
    if task == "chi_squared":
        return _chi_squared(args, context)
    return _hardy_weinberg(args, context)


# --- bio_calc ---------------------------------------------------------------------------------------------------------


def bio_calc(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    task = args["task"]
    if task == "magnification":
        image = Fraction(str(args["image_length"])) * MICROMETRES[args["image_unit"]]
        actual = Fraction(str(args["actual_length"])) * MICROMETRES[args["actual_unit"]]
        result, unit = image / actual, "times"
        convention = "both lengths in micrometres (1 mm = 1000 um); magnification = image size / actual size"
    elif task == "percentage_change":
        initial, final = Fraction(str(args["initial"])), Fraction(str(args["final"]))
        if initial == 0:
            raise _domain("a percentage change from zero is undefined")
        result, unit = 100 * (final - initial) / initial, "%"
        convention = "percentage change = 100 x (final - initial) / initial"
    elif task == "rate":
        result, unit = Fraction(str(args["change"])) / Fraction(str(args["duration"])), str(args["unit"])
        convention = f"rate = change / time, in {args['unit']}"
    elif task == "recapture":
        first, second, recaptured = args["marked_first"], args["second_catch"], args["marked_second"]
        if recaptured > second or recaptured > first:
            raise _domain("recaptured marked individuals cannot exceed the second catch or the number marked")
        result, unit = Fraction(first * second, recaptured), "individuals"
        convention = "Lincoln-Petersen: N = marked first x second catch / marked in second catch (not rounded)"
    else:
        counts = args["counts"]
        total = sum(counts)
        if total < 2:
            raise _domain("Simpson's index needs at least two individuals")
        result = 1 - Fraction(sum(n * (n - 1) for n in counts), total * (total - 1))
        unit = "index"
        convention = "Simpson's index of diversity D = 1 - sum n(n-1) / N(N-1)"
    value = {"task": task, "result": _checked(float(result), unit, context), "convention": convention}
    return value, {"method": "exact", "confidence": "proved"}

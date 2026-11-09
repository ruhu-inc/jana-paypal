"""The worker's static function table (TOL-05): every function the worker can run, by name, with its version and
argument schema. A change to a function's schema, defaults or output semantics increments its version."""

from __future__ import annotations

from tool_worker.functions import (
    acidbase,
    alevel,
    alevel_calculus,
    answers,
    appraisal,
    biology,
    calculus,
    cashflow,
    chemistry,
    circuits,
    distributions,
    energetics,
    equilibrium,
    geometry,
    ledger,
    maths,
    mechanics,
    optics,
    plotting,
    projectile,
    ratios,
    statements,
    stats,
    steps,
    stoichiometry,
    units,
    valuation,
)
from tool_worker.registry import Function, Implementation, load_schema


def _function(name: str, version: int, implementation: Implementation) -> Function:
    return Function(name, version, load_schema(name), implementation)


FUNCTIONS: dict[str, Function] = {
    f.name: f
    for f in (
        _function("math_parse", 1, maths.math_parse),
        _function("math_solve", 4, maths.math_solve),  # 4: complex roots and numerical methods (VER-30)
        _function("math_simplify", 2, maths.math_simplify),  # 2: partial fractions, series, complex (VER-30)
        _function("math_check_equivalence", 1, maths.math_check_equivalence),
        _function("math_check_step", 3, steps.math_check_step),
        _function("math_evaluate", 2, maths.math_evaluate),
        _function("units_convert", 1, units.units_convert),
        _function("units_check_dimensions", 1, units.units_check_dimensions),
        # physics (VER-16, VER-17; M12.b)
        _function("phys_optics", 1, optics.phys_optics),
        _function("phys_circuit", 1, circuits.phys_circuit),
        _function("phys_projectile", 2, projectile.phys_projectile),  # 2: angled launches
        # M12.a: mathematics checkers of stage S2 (VER-08, VER-09, VER-10, VER-13, VER-14, VER-15)
        _function("math_differentiate", 2, calculus.math_differentiate),  # 2: implicit, parametric (VER-30)
        _function("math_integrate", 2, calculus.math_integrate),  # 2: volumes, trapezium, substitution, parts
        _function("math_check_answer", 1, answers.math_check_answer),
        _function("stats_compute", 1, stats.stats_compute),
        _function("math_plot_points", 1, plotting.math_plot_points),
        _function("geom_figure", 1, geometry.geom_figure),
        # M12.d: accounting checkers of stage S2 (VER-22, VER-23, VER-24, VER-25)
        _function("acc_double_entry", 2, ledger.acc_double_entry),  # 2: control accounts (VER-34)
        _function("acc_statements", 2, alevel.acc_statements),  # 2: the A level forms (VER-34)
        _function("acc_depreciation", 1, valuation.acc_depreciation),
        _function("acc_inventory", 1, valuation.acc_inventory),
        _function("acc_ratios", 1, ratios.acc_ratios),
        _function("acc_break_even", 1, ratios.acc_break_even),
        # M26: accounting checkers of stage S3 (VER-34)
        _function("acc_cash_flow", 1, cashflow.acc_cash_flow),
        _function("acc_appraisal", 1, appraisal.acc_appraisal),
        # M12.c: chemistry checkers of stage S2 (VER-18, VER-19, VER-20, VER-21)
        _function("chem_molar_mass", 1, chemistry.chem_molar_mass),
        _function("chem_balance", 1, chemistry.chem_balance),
        _function("chem_stoichiometry", 1, stoichiometry.chem_stoichiometry),
        _function("chem_acid_base", 2, acidbase.chem_acid_base),  # 2: `ka` for a weak acid (VER-32)
        _function("chem_energetics", 1, energetics.chem_energetics),
        # M26.a: science checkers of stage S3 on the science-compute contract (VER-32, VER-35, VER-38)
        _function("bio_genetics", 1, biology.bio_genetics),
        _function("bio_calc", 1, biology.bio_calc),
        _function("chem_equilibrium", 1, equilibrium.chem_equilibrium),
        # M26.a: A level mechanics of stage S3 (VER-31)
        _function("phys_mechanics", 1, mechanics.phys_mechanics),
        # M26.a: A level calculus of stage S3 (VER-30)
        _function("math_solve_ode", 1, alevel_calculus.math_solve_ode),
        # M26.a: A level statistics of stage S3 (VER-30)
        _function("stats_distribution", 1, distributions.stats_distribution),
    )
}


def preload() -> None:
    """Import and warm the libraries the functions use, before the import allow-list is installed (TOL-26): the
    maths libraries and the generated contract bindings with their runtime (SEC-07). Bytecode cannot be loaded once
    the guard is in place, so everything the worker needs is imported here."""
    import importlib
    import pkgutil

    import tool_worker.contracts as bindings
    from tool_worker.registry import CallContext

    for module in pkgutil.walk_packages(bindings.__path__, prefix="tool_worker.contracts."):
        importlib.import_module(module.name)

    context = CallContext("preload", "chk_01JZ0000000000000000000000", {}, {}, lambda: False, settings={})
    maths.math_check_equivalence({"a": "sin(x)^2 + cos(x)^2", "b": "1"}, context)
    maths.math_solve({"equations": ["2x^2 - 8x + 6 = 0"]}, context)
    maths.math_evaluate({"expression": "sqrt(2) + log(10)", "sf": 3}, context)
    steps.math_check_step({"prev": "x - 3 = 5", "next": "x = 2"}, context)
    units.units_convert({"quantity": "72 km/h", "to": "m/s"}, context)
    units.units_check_dimensions({"quantity": "9.8 m s^-2", "expected": "N/kg"}, context)
    # physics (M12.b): quantities in equations, surds and inverse trig, and the circuit solver
    maths.math_solve({"equations": ["v = u + a*t"], "unknowns": ["v"], "units": {"v": "km/h"},
                      "quantities": {"u": "5 m/s", "a": "2 m/s^2", "t": "3 s"}}, context)  # fmt: skip
    optics.phys_optics({"optic": "converging_lens", "focal_length": 10, "object_distance": 15, "object_height": 2,
                        "sign_convention": "cartesian"}, context)  # fmt: skip
    optics.phys_optics({"task": "refraction", "n1": 1, "angle1": 40, "n2": "1.5"}, context)
    circuits.phys_circuit({"components": [{"id": "B", "kind": "cell", "emf": 6, "internal_resistance": 1},
                                          {"id": "R", "kind": "resistor", "resistance": 2}],
                           "network": {"series": ["B", "R"]}}, context)  # fmt: skip
    projectile.phys_projectile({"motion": "horizontal", "speed": "72 km/h", "height": 5, "g": 10, "g_source": "pack",
                                "points": 3}, context)  # fmt: skip
    # M12.a: the calculus, limits, continuous-domain and trigonometric paths SymPy imports lazily.
    calculus.math_differentiate({"expression": "x^3 - 3x", "stationary_points": True, "at": 1}, context)
    calculus.math_integrate({"expression": "x^2 - 4", "lower": -3, "upper": 3, "area": True, "student": "x"}, context)
    calculus.math_integrate({"expression": "sqrt(x)", "lower": 0, "upper": 4}, context)  # rational powers
    # VER-30: implicit and parametric derivatives, volumes, the trapezium rule, substitution, parts and separable ODEs
    calculus.math_differentiate({"mode": "implicit", "expression": "x^2 + y^2 = 25", "point": [3, 4]}, context)
    calculus.math_differentiate({"mode": "parametric", "x_expression": "t^2", "y_expression": "sin(t)", "order": 2,
                                 "at": "pi/2"}, context)  # fmt: skip
    calculus.math_integrate({"expression": "e^x", "volume": "x_axis", "lower": 0, "upper": "ln(2)"}, context)
    calculus.math_integrate({"expression": "1/(1 + x^2)", "method": "trapezium", "strips": 4, "lower": 0,
                             "upper": 1}, context)  # fmt: skip
    calculus.math_integrate({"expression": "x (x^2 + 1)^3", "substitution": "u = x^2 + 1", "lower": 0, "upper": 1},
                            context)  # fmt: skip
    calculus.math_integrate({"expression": "x e^x", "parts_u": "x"}, context)
    alevel_calculus.math_solve_ode({"rhs": "x y", "condition": [0, 2], "at": 1}, context)
    # VER-30: series, partial fractions, complex numbers and numerical methods
    maths.math_simplify({"expression": "(5x+3)/((1+x)(1-2x))", "form": "series", "order": 2}, context)
    maths.math_simplify({"expression": "1/((x-1)^2(x^2+1))", "form": "partial_fractions"}, context)
    maths.math_simplify({"expression": "-3+4i", "form": "modulus_argument"}, context)
    maths.math_solve({"equations": ["z^3 = 1"], "domain": "complex"}, context)
    maths.math_solve({"equations": ["x^3 - 2x - 5 = 0"], "method": "iteration", "g": "cbrt(2x+5)", "start": 2,
                      "steps": 3}, context)  # fmt: skip
    answers.math_check_answer({"student": "2(x+3)", "key": "2x+6", "rule": {"form": "factorised"}}, context)
    answers.math_check_answer({"student": "2 km", "key": "2000 m"}, context)
    stats.stats_compute({"data": [1, 2, 2, 5]}, context)
    plotting.math_plot_points({"curves": [{"id": "f", "expr": "1/(x-2) + sin(x)"}, {"id": "g", "expr": "sqrt(x)"}],
                               "x_range": [-5, 5], "max_points": 40}, context)  # fmt: skip
    geometry.geom_figure({"template": "triangle", "givens": {"A": 30, "a": 5, "b": 8}}, context)
    geometry.geom_figure({"template": "transformation", "givens": {"points": [[1, 1]], "transformation": {
        "kind": "reflection", "line": "y = x"}}}, context)  # fmt: skip
    # M12.d: the accounting checkers, including the reconciliation's line alignment (difflib).
    ledger.acc_double_entry({"entries_text": "Dr Equipment 5000; Cr Cash 2500; Cr Payables 2500",
                             "key_text": "Dr Equipment 5000; Cr Cash 5000"}, context)  # fmt: skip
    ledger.acc_double_entry({"cash_book": {"balance": 100, "entries": [{"direction": "out", "amount": 20}]},
                             "bank_statement": {"balance": 120}, "student": [{"amount": 120}]}, context)  # fmt: skip
    trial_balance = [{"account": "Sales", "role": "revenue", "credit": 10},
                     {"account": "Cash", "role": "cash", "debit": 10}]  # fmt: skip
    statements.acc_statements({"trial_balance": trial_balance}, context)
    valuation.acc_depreciation({"method": "reducing_balance", "cost": 1000, "rate_percent": 20, "years": 2}, context)
    valuation.acc_inventory({"method": "avco", "movements": [{"kind": "opening", "quantity": 3, "unit_cost": 1},
                                                             {"kind": "issue", "quantity": 1}]}, context)  # fmt: skip
    ratios.acc_ratios({"revenue": 100, "cost_of_sales": 60}, context)
    ratios.acc_break_even({"fixed_costs": 100, "variable_cost_per_unit": 6, "selling_price": 10}, context)
    # M12.c: the formula grammar, the null space, unit conversions and exact logarithms the chemistry checkers use.
    chemistry.chem_molar_mass({"formula": "CuSO4·5H2O"}, context)
    chemistry.chem_balance({"equation": "Fe^3+ + OH^- -> Fe(OH)3", "coefficients": [1, 3, 1]}, context)
    stoichiometry.chem_stoichiometry({"equation": "2Mg + O2 -> 2MgO", "steps": [
        {"id": "n", "op": "amount_from_concentration", "concentration": "0.5 M", "volume": "250 cm^3"},
        {"id": "m", "op": "mole_ratio", "from": "O2", "to": "MgO", "amount": "@n"},
        {"id": "v", "op": "gas_volume_from_amount", "amount": "@m"}]}, context)  # fmt: skip
    acidbase.chem_acid_base({"acid": "HCl", "acid_concentration": "0.1 M", "acid_volume": "25 cm^3", "base": "NaOH",
                             "base_concentration": "0.1 M", "points": 5}, context)  # fmt: skip
    energetics.chem_energetics({"mass": "50 g", "specific_heat": "4.2 J/(g K)", "initial_temperature": "20 °C",
                                "final_temperature": "26 °C", "amount": "0.025 mol"}, context)  # fmt: skip
    # M26.a: A level statistics: erf and erfinv, and the incomplete beta function of the PMCC critical value.
    distributions.stats_distribution({"task": "normal", "mean": 50, "sd": 4, "relation": "lt", "x": 53}, context)
    distributions.stats_distribution({"task": "normal", "find": "value", "mean": 50, "sd": 4, "probability": "0.05",
                                      "tail": "above"}, context)  # fmt: skip
    distributions.stats_distribution({"task": "hypothesis_test", "test": "pmcc", "alternative": "greater",
                                      "significance_level": "0.05", "r": "0.6", "n": 10}, context)  # fmt: skip

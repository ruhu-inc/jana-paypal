"""A content pack's mastery-params file (LRN-11): the fitted parameters a pack ships, checked before they are used.

The file rides inside the signed pack as an embedded signed file of kind `mastery_params` (its sha256 is in the signed
manifest, so the pack's signature covers it; `store.packs.verify` checks it). Its shape follows
the `learning/mastery-params` contract (D99): `schema_version`, `pack`, `params_version`, `fitted` and a `mastery`
section with the model, the Elo constants, the per-KC defaults and the fitted per-KC values. A file that fails the
contract, or any of whose values falls outside the range of the configuration key it sets, fails: the pack is rejected
and the parameters already active stay in force (LRN-11 `lrn11_params_signed_and_valid`).

Only the values that are per KC (`kc_overrides`) and the model choice per subject are applied per pack; the rest of the
file must agree with the build's keys within their declared ranges (one parameter file per learner database, LRN-02).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from agent_core.config.keys import Settings
from agent_core.contracts import errors as contract_errors

KIND = "mastery_params"
CONTRACT = "learning/mastery-params"
# Where each value of the file goes: its path in the document and the configuration key whose range it must meet.
KEYS: dict[tuple[str, ...], str] = {
    ("mastery", "count_half_life_days"): "mastery_half_life_days",
    ("mastery", "elo", "k_a"): "mastery_k_a",
    ("mastery", "elo", "k_c"): "mastery_k_c",
    ("mastery", "kc_defaults", "beta"): "mastery_beta_default",
    ("mastery", "kc_defaults", "gamma"): "mastery_gamma_default",
    ("mastery", "kc_defaults", "rho"): "mastery_rho_default",
    ("mastery", "bkt_baseline", "p_init"): "bkt_p_init",
    ("mastery", "bkt_baseline", "p_transit"): "bkt_p_transit",
    ("mastery", "bkt_baseline", "p_slip"): "bkt_p_slip",
    ("mastery", "bkt_baseline", "p_guess"): "bkt_p_guess",
    ("mastery", "thresholds", "ready_to_check"): "mastery_ready_p",
    ("mastery", "thresholds", "ready_min_n_eff"): "mastery_ready_n_eff",
    ("mastery", "thresholds", "needs_review_retrievability"): "mastery_review_retrievability",
}


def _get(doc: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = doc
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def problems(doc: Any) -> list[str]:
    """Why a mastery-params document cannot be used; empty when it can."""
    if not isinstance(doc, Mapping):
        return ["not an object"]
    out = contract_errors(CONTRACT, dict(doc))
    if out:
        return out
    for path, key in KEYS.items():
        value = _get(doc, path)
        if value is None:
            continue
        try:
            Settings.model_validate({key: value})
        except ValidationError:
            out.append(f"{'.'.join(path)} is outside the range of {key}")
    for kc, values in doc["mastery"].get("kc_overrides", {}).items():
        for name, key in (("beta", "mastery_beta_default"), ("gamma", "mastery_gamma_default"),
                          ("rho", "mastery_rho_default")):  # fmt: skip
            try:
                Settings.model_validate({key: values[name]})
            except ValidationError:
                out.append(f"kc_overrides: {kc}.{name} is outside the range of {key}")
    return out


def kc_overrides(doc: Mapping[str, Any]) -> dict[str, tuple[float, float, float]]:
    """The fitted (beta, gamma, rho) per KC of a valid document."""
    rows = (doc.get("mastery") or {}).get("kc_overrides") or {}
    return {str(kc): (float(v["beta"]), float(v["gamma"]), float(v["rho"])) for kc, v in rows.items()}


def model_of(doc: Mapping[str, Any]) -> str:
    return str((doc.get("mastery") or {}).get("model") or "pfa_elo")

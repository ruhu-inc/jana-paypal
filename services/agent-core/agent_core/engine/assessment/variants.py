"""Numeric practice variants with worker-computed keys (ASM-22, CUR-17 subset)."""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Any

from agent_core.engine.assessment.errors import Refused
from agent_core.store.assessment import dump


class VariantCommands:
    async def generate_variant(self: Any, item_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        base = self.content.record("items", item_id)
        template = self.content.record("item_templates", str((base or {}).get("template_id", "")))
        if base is None or template is None or self.run_tool is None or item_id in self._reserved():
            raise Refused("variant_unavailable")
        if base["type"] != "numeric" or len(base["parts"]) != 1 or template.get("constraints"):
            raise Refused("variant_not_supported")
        declared = {p["name"]: p for p in template["parameters"]}
        if set(parameters) != set(declared):
            raise Refused("variant_parameters_invalid")
        for name, value in parameters.items():
            spec = declared[name]
            if spec["kind"] == "choice":
                if value not in spec["choices"]:
                    raise Refused("variant_parameter_out_of_range")
            else:
                try:
                    numeric = Decimal(str(value))
                    low, high = Decimal(str(spec["min"])), Decimal(str(spec["max"]))
                    if not numeric.is_finite() or not low <= numeric <= high:
                        raise Refused("variant_parameter_out_of_range")
                    if spec["kind"] == "integer" and numeric != numeric.to_integral_value():
                        raise Refused("variant_parameter_out_of_range")
                    if spec.get("step") is not None and (numeric - low) % Decimal(str(spec["step"])):
                        raise Refused("variant_parameter_out_of_range")
                except (InvalidOperation, KeyError):
                    raise Refused("variant_parameter_out_of_range") from None
        tool = template["answer_tool"]
        if tool["tool"] != "math_evaluate":
            raise Refused("variant_not_supported")

        def render(value: Any) -> Any:
            if isinstance(value, str):
                return value.format_map(parameters)
            if isinstance(value, dict):
                return {k: render(v) for k, v in value.items()}
            if isinstance(value, list):
                return [render(v) for v in value]
            return value

        try:
            args, stem = render(tool["args"]), str(render(template["stem_template"]))
        except (KeyError, ValueError):
            raise Refused("variant_not_supported") from None
        result, check_id = await self.run_tool("math_evaluate", args)
        text = str(((result or {}).get("exact") or {}).get("text", ""))
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise Refused("variant_key_unverified") from None
        if not check_id or not number.is_finite() or (result or {}).get("status") != "ok":
            raise Refused("variant_key_unverified")
        variant = copy.deepcopy(base)
        variant["stem"] = stem
        variant["parts"][0]["answer"].update(value=str(number), check_id=check_id)
        self.variant_proofs.add((dump(variant), check_id))
        return dict(self.accept_variant(variant, check_id))

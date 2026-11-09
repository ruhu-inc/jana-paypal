"""Optics (VER-16): thin lenses and curved mirrors, Snell's law, refractive index and critical angle.

Image formation works on physical distances (object and focal length as magnitudes, the optic giving the focal
length's sign) and reports signed distances in the sign convention stated with the call, `real_is_positive` or
`cartesian`, which is a pack convention (CUR-28) recorded in the result with its source. There is no tool default
for the convention: without one the result is `unknown` with `convention_missing`, never a guess (as VER-12 does for
constants). Magnification is the ratio of image to object height, and the image's nature (real or virtual, upright
or inverted, magnified or diminished) is reported in words, so neither depends on the convention.

The three principal rays for the ray diagram are drawn in a fixed frame: the optic on the y axis at x = 0, the
principal axis along y = 0, the object to the left with its top at (-object_distance, object_height) and incident
light travelling in +x. Rays are thin-optic constructions (deviation at x = 0), with dashed virtual extensions back
to a virtual image. Angles are in degrees, measured from the normal."""

from __future__ import annotations

from typing import Any

import sympy as sp

from tool_worker.functions.maths import exact, significant
from tool_worker.functions.quantities import number
from tool_worker.protocol import Code, WorkerError
from tool_worker.registry import CallContext

Extras = dict[str, Any]
# optic -> (lens or mirror, sign of its focal length with real distances positive)
OPTICS = {
    "converging_lens": ("lens", 1),
    "diverging_lens": ("lens", -1),
    "concave_mirror": ("mirror", 1),
    "convex_mirror": ("mirror", -1),
    "plane_mirror": ("mirror", 0),
}
FORMULAS = {
    ("real_is_positive", "lens"): "1/u + 1/v = 1/f",
    ("real_is_positive", "mirror"): "1/u + 1/v = 1/f",
    ("cartesian", "lens"): "1/v - 1/u = 1/f",
    ("cartesian", "mirror"): "1/v + 1/u = 1/f",
}
PROVED = {"method": "exact", "confidence": "proved"}


def _positive(args: dict[str, Any], field: str) -> sp.Rational:
    if field not in args:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field} is needed")
    value = number(args[field], field)
    if value <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field} is a distance or size and must be positive")
    return value


def _xy(x: sp.Basic, y: sp.Basic) -> list[float]:
    return [float(significant(x, 6)), float(significant(y, 6))]


def phys_optics(args: dict[str, Any], context: CallContext) -> tuple[Any, Extras]:
    task = args.get("task", "image")
    if task == "refraction":
        return _refraction(args)
    if task == "critical_angle":
        return _critical_angle(args)
    return _image(args)


# --- lenses and mirrors ---------------------------------------------------------------------------------------


def _image(args: dict[str, Any]) -> tuple[dict[str, Any], Extras]:
    if "optic" not in args:
        raise WorkerError(Code.DOMAIN_ERROR, "optic is needed for an image")
    kind, sign = OPTICS[args["optic"]]
    convention = args.get("sign_convention")
    if convention is None:
        return {"status": "unknown", "reason": "convention_missing"}, {
            "method": "heuristic", "confidence": "undecided",
            "warnings": [{"code": "convention_missing", "message": "the optics sign convention is not stated (no "
                          "tool default): give real_is_positive or cartesian from the pack or the question"}],
        }  # fmt: skip
    d_o = _positive(args, "object_distance")
    f = sign * _positive(args, "focal_length") if sign else None  # real-is-positive focal length
    at_infinity = f is not None and d_o == f
    d_i = None if at_infinity else (-d_o if f is None else f * d_o / (d_o - f))  # real-is-positive image distance
    stated = {"value": convention, **({"source": args["sign_convention_source"]}
                                     if "sign_convention_source" in args else {})}  # fmt: skip
    value: dict[str, Any] = {"task": "image", "optic": args["optic"], "sign_convention": stated,
                             "formula": FORMULAS[(convention, kind)]}  # fmt: skip
    if args.get("distance_unit"):
        value["distance_unit"] = args["distance_unit"]
    # Real-is-positive distances in the stated convention: cartesian measures along the incident light.
    flip_object = -1 if convention == "cartesian" else 1
    flip_image = -1 if convention == "cartesian" and kind == "mirror" else 1
    value["object_distance"] = exact(flip_object * d_o)
    if f is not None:
        value["focal_length"] = exact(flip_image * f)
    if d_i is None:
        value["image"] = "at_infinity"
    else:
        magnification = abs(d_i) / d_o
        real = d_i > 0
        size = "same_size" if magnification == 1 else ("magnified" if magnification > 1 else "diminished")
        value["image"] = "formed"
        value["image_distance"] = exact(flip_image * d_i)
        value["magnification"] = exact(magnification)
        value["nature"] = {"type": "real" if real else "virtual",
                           "orientation": "inverted" if real else "upright", "size": size}  # fmt: skip
        if "object_height" in args:
            value["image_height"] = exact(magnification * _positive(args, "object_height"))
    if "object_height" in args:
        value["rays"], value["frame"] = _rays(kind, f, d_o, d_i, _positive(args, "object_height"))
    working = [{"label": value["formula"], "value": value.get("image_distance", {}).get("text", "infinity")}]
    return value, {**PROVED, "working": working}


def _rays(
    kind: str, f: sp.Rational | None, d_o: sp.Rational, d_i: sp.Basic | None, h: sp.Rational
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The principal rays from the object's top in the drawing frame described in the module docstring."""
    out_dir = 1 if kind == "lens" else -1  # light leaves a lens in +x and a mirror in -x
    image: tuple[sp.Basic, sp.Basic] | None = None
    if d_i is not None:
        image = (out_dir * d_i, -h * d_i / d_o)
    # Where each ray meets the optic: parallel at the object's height, focal through (or towards) the focal point on
    # the incident side of the construction, centre through the optical centre (lens) or centre of curvature (mirror).
    meets: list[tuple[str, sp.Basic]] = [("parallel", h)]
    if f is None:
        meets.append(("pole", sp.Integer(0)))
    else:
        if d_o != f:
            meets.append(("focal", -h * f / (d_o - f)))
        if kind == "lens":
            meets.append(("centre", sp.Integer(0)))
        elif d_o != 2 * f:
            meets.append(("centre", -2 * h * f / (d_o - 2 * f)))
    reach = max(d_o, 2 * abs(f) if f is not None else d_o, abs(image[0]) if image else 0)
    end_x = out_dir * reach
    rays = []
    for name, y_at in meets:
        ray: dict[str, Any] = {"ray": name}
        start, hit = _xy(-d_o, h), _xy(0, y_at)
        if image is not None and image[0] * out_dir > 0:  # a real image: the ray passes through it
            ray["path"] = [start, hit, _xy(*image)]
        elif image is not None:  # a virtual image: the ray leaves along the line from the image through the hit
            slope = (y_at - image[1]) / (0 - image[0])
            ray["path"] = [start, hit, _xy(end_x, y_at + slope * end_x)]
            ray["virtual"] = [hit, _xy(*image)]
        else:  # image at infinity: every ray leaves parallel to the deviated parallel ray, which passes through F
            assert f is not None
            slope = -h / (out_dir * f)
            ray["path"] = [start, hit, _xy(end_x, y_at + slope * end_x)]
        rays.append(ray)
    frame: dict[str, Any] = {"optic_x": 0, "light_in": "+x", "object_top": _xy(-d_o, h),
                             "image_top": _xy(*image) if image else None}  # fmt: skip
    if f is not None:
        frame["focal_points"] = [_xy(-f, 0), _xy(f, 0)] if kind == "lens" else [_xy(-f, 0)]
        if kind == "mirror":
            frame["centre_of_curvature"] = _xy(-2 * f, 0)
    return rays, frame


# --- refraction -------------------------------------------------------------------------------------------------


def _angle(args: dict[str, Any], field: str) -> sp.Rational:
    value = number(args[field], field)
    if not 0 <= value < 90:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field} is measured from the normal and must be from 0 to below 90")
    return value


def _index(args: dict[str, Any], field: str) -> sp.Rational:
    value = number(args[field], field)
    if value <= 0:
        raise WorkerError(Code.DOMAIN_ERROR, f"{field} must be positive")
    return value


def _sin(degrees: sp.Basic) -> sp.Basic:
    return sp.sin(degrees * sp.pi / 180)


def _asin(ratio: sp.Basic) -> sp.Basic:
    return sp.asin(ratio) * 180 / sp.pi


def _refraction(args: dict[str, Any]) -> tuple[dict[str, Any], Extras]:
    """Snell's law n1 sin(angle1) = n2 sin(angle2), for whichever one of the four is not given."""
    fields = ("n1", "angle1", "n2", "angle2")
    missing = [name for name in fields if name not in args]
    if len(missing) != 1:
        raise WorkerError(Code.DOMAIN_ERROR, "give exactly three of n1, angle1, n2 and angle2")
    n1 = _index(args, "n1") if "n1" in args else None
    n2 = _index(args, "n2") if "n2" in args else None
    a1 = _angle(args, "angle1") if "angle1" in args else None
    a2 = _angle(args, "angle2") if "angle2" in args else None
    value: dict[str, Any] = {"task": "refraction", "case": "refracted", "angle_unit": "degrees"}
    if a2 is None:
        assert n1 is not None and n2 is not None and a1 is not None
        ratio = n1 * _sin(a1) / n2
        if ratio > 1:  # beyond the critical angle: no refracted ray
            value.update(case="total_internal_reflection", critical_angle=exact(_asin(n2 / n1)))
            value.update(n1=exact(n1), n2=exact(n2), angle1=exact(a1))
            return value, PROVED
        a2 = _asin(ratio)
    elif a1 is None:
        assert n1 is not None and n2 is not None
        ratio = n2 * _sin(a2) / n1
        if ratio > 1:
            raise WorkerError(Code.DOMAIN_ERROR, "no angle of incidence gives this angle of refraction")
        a1 = _asin(ratio)
    elif n2 is None:
        assert n1 is not None
        if a2 == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a ray along the normal does not determine a refractive index")
        n2 = n1 * _sin(a1) / _sin(a2)
    else:
        if a1 == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a ray along the normal does not determine a refractive index")
        n1 = n2 * _sin(a2) / _sin(a1)
    assert n1 is not None and n2 is not None and a1 is not None and a2 is not None
    bends = "undeviated" if a1 == 0 or n1 == n2 else ("towards_normal" if n2 > n1 else "away_from_normal")
    value.update(n1=exact(n1), angle1=exact(a1), n2=exact(n2), angle2=exact(a2), bends=bends)
    return value, {**PROVED, "working": [{"label": "n1 sin(angle1) = n2 sin(angle2)", "value": missing[0]}]}


def _critical_angle(args: dict[str, Any]) -> tuple[dict[str, Any], Extras]:
    """sin C = n2 / n1 from the denser medium (n1) to the less dense (n2); n2 is 1 when n1 is the index relative to
    the outer medium. Either n1 or the critical angle (angle1) is given and the other is found."""
    n2 = _index(args, "n2") if "n2" in args else sp.Integer(1)
    if ("n1" in args) == ("angle1" in args):
        raise WorkerError(Code.DOMAIN_ERROR, "give either n1 or the critical angle as angle1")
    if "n1" in args:
        n1 = _index(args, "n1")
        if n1 <= n2:
            raise WorkerError(Code.DOMAIN_ERROR, "there is no critical angle unless n1 is greater than n2")
        critical = _asin(n2 / n1)
    else:
        critical = _angle(args, "angle1")
        if critical == 0:
            raise WorkerError(Code.DOMAIN_ERROR, "a critical angle of 0 has no refractive index")
        n1 = n2 / _sin(critical)
    value: dict[str, Any] = {
        "task": "critical_angle",
        "n1": exact(n1),
        "n2": exact(n2),
        "critical_angle": exact(critical),
        "angle_unit": "degrees",
    }
    return value, {**PROVED, "working": [{"label": "sin C = n2 / n1", "value": value["critical_angle"]["text"]}]}

"""The worker loads only its own locked dependency closure (PLT-03, D36).

The shipped bundle holds `jana-tool-worker`'s closure and nothing else, but a development worker runs from the shared
environment, where other services' dependencies are installed too (the perception worker's NumPy and OpenCV, for
one). Libraries in the closure import some packages only when they are present (Pint and SymPy try NumPy), so a
development worker would load modules the bundle never has, under the same memory limit. `confine()` runs before the
libraries are preloaded and hides every top-level module that is neither standard library nor provided by a
distribution in the closure: such an import fails as if the module were not installed, exactly as in the bundle.

In the bundle the worker's distribution metadata is absent and everything installed is the closure, so there is
nothing to hide and `confine()` does nothing.
"""

from __future__ import annotations

import importlib.machinery
import os
import re
import sys
import sysconfig
from collections.abc import Sequence
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

WORKER_DISTRIBUTION = "jana-tool-worker"
WORKER_PACKAGE = "tool_worker"


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str | None:
    """The distribution a requirement names, or None when it applies to an extra only."""
    spec, _, marker = requirement.partition(";")
    if re.search(r"\bextra\b", marker):
        return None
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return _normalise(match.group(1)) if match else None


def _top_level(dist: metadata.Distribution) -> set[str]:
    """The distribution's top-level importable modules, from its installed file list."""
    found = set()
    for file in dist.files or ():
        top = file.parts[0]
        if top in ("..", "__pycache__") or top.endswith((".dist-info", ".data", ".pth")):
            continue
        if len(file.parts) == 1:  # a single-file module or an extension module (name.cpython-312-darwin.so)
            if not top.endswith((".py", ".so", ".pyd")):
                continue
            top = top.split(".", 1)[0]
        if top.isidentifier():
            found.add(top)
    return found


def closure_modules() -> frozenset[str] | None:
    """Top-level modules of the worker's installed dependency closure; None where the worker's metadata is absent
    (the bundle)."""
    try:
        metadata.distribution(WORKER_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    modules = {WORKER_PACKAGE}
    seen: set[str] = set()
    pending = [_normalise(WORKER_DISTRIBUTION)]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        modules |= _top_level(dist)
        pending += [n for r in dist.requires or () if (n := _requirement_name(r))]
    return frozenset(modules)


class ConfinedFinder:
    """Wraps one of the import system's finders: a top-level module that is neither standard library nor in the
    closure is not found (`None`, as for a module that is not installed, so both `import` and
    `importlib.util.find_spec` see it as absent); everything else, submodules included, is the wrapped finder's."""

    def __init__(self, inner: Any, modules: frozenset[str], stdlib: tuple[str, ...]) -> None:
        self.inner = inner
        self.modules = modules
        self.stdlib = stdlib

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        spec: importlib.machinery.ModuleSpec | None = self.inner.find_spec(fullname, path, target)
        if spec is None or path is not None or fullname in self.modules or fullname in sys.stdlib_module_names:
            return spec
        places = [spec.origin or "", *(spec.submodule_search_locations or ())]
        return spec if all(self._in_stdlib(p) for p in places if p) else None

    def _in_stdlib(self, place: str) -> bool:
        real = os.path.realpath(place)
        return "site-packages" not in Path(real).parts and any(
            real == root or real.startswith(root + os.sep) for root in self.stdlib
        )

    def invalidate_caches(self) -> None:
        if hasattr(self.inner, "invalidate_caches"):
            self.inner.invalidate_caches()

    def __getattr__(self, name: str) -> Any:  # find_distributions and the rest of the wrapped finder
        return getattr(self.inner, name)


def confine() -> frozenset[str] | None:
    """Hide what the closure does not provide (idempotent); returns the closure's modules, or None in the bundle."""
    for finder in sys.meta_path:
        if isinstance(finder, ConfinedFinder):
            return finder.modules
    modules = closure_modules()
    if modules is not None:
        stdlib = tuple(sorted({os.path.realpath(sysconfig.get_path(k)) for k in ("stdlib", "platstdlib")}))
        builtin = (importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter)
        sys.meta_path[:] = [f if f in builtin else ConfinedFinder(f, modules, stdlib) for f in sys.meta_path]
    return modules


def hidden(module: str) -> bool:
    """Whether `confine()` hides this top-level module (never in the bundle or before `confine()`)."""
    return any(isinstance(f, ConfinedFinder) and module not in f.modules for f in sys.meta_path)

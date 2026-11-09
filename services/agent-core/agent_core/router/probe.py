"""The hardware probe, the benchmark and the hardware class (MDL-16, PLT-08, PLT-29; TABLE MDL-16a, TABLE PLT-08a).

The probe reads what the machine has: installed memory, CPU cores, architecture and instruction sets, the CPU's
release year when its model name says it, GPUs with their memory, and free disk. The benchmark measures prompt and
generation speed with a short generation on the smallest installed model. The class is the highest one whose entry
rule holds; a machine below the minimum laptop is content-only, and the checks it failed are kept so the app can name
the figure that is short ("This computer has 4 GB of memory; the tutor needs 8 GB"). Without a benchmark (no model
installed yet) the class is predicted from the hardware alone and marked provisional. Everything here runs offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from agent_core.config import Settings
from agent_core.providers.base import ProviderError

GB = 1024**3
CLASSES = ("content_only", "low", "mid", "high")
GpuKind = Literal["discrete", "integrated", "apple"]


@dataclass(frozen=True)
class Gpu:
    kind: GpuKind
    name: str
    memory_bytes: int  # dedicated memory, or the unified memory an Apple GPU shares with the CPU


@dataclass(frozen=True)
class Probe:
    os: str  # macos, windows or linux
    arch: str  # x86_64, arm64, or a 32-bit name
    ram_bytes: int
    cpu_cores: int
    cpu_model: str
    cpu_features: frozenset[str]
    free_disk_bytes: int
    gpus: tuple[Gpu, ...] = ()
    available_bytes: int | None = None  # memory free for a new process now (ARC-31); None when not read

    @property
    def cpu_year(self) -> int | None:
        return cpu_year(self.cpu_model, self.arch)

    @property
    def apple_silicon(self) -> bool:
        return self.os == "macos" and self.arch == "arm64"

    @property
    def discrete_gpu(self) -> Gpu | None:
        return next((g for g in self.gpus if g.kind == "discrete"), None)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["cpu_features"] = sorted(self.cpu_features)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Probe:
        gpus = tuple(Gpu(g["kind"], g["name"], int(g["memory_bytes"])) for g in data.get("gpus", []))
        available = data.get("available_bytes")
        return cls(data["os"], data["arch"], int(data["ram_bytes"]), int(data["cpu_cores"]), data["cpu_model"],
                   frozenset(data["cpu_features"]), int(data["free_disk_bytes"]), gpus,
                   int(available) if available is not None else None)  # fmt: skip

    def fingerprint(self) -> str:
        """What a hardware change means (PLT-09): memory, CPU or GPU, never free disk or free memory, which change
        all the time. Installed memory is rounded to the GB so a firmware reservation does not count as a change."""
        parts = {"os": self.os, "arch": self.arch, "ram_gb": round(self.ram_bytes / GB), "cpu": self.cpu_model,
                 "cores": self.cpu_cores, "features": sorted(self.cpu_features),
                 "gpus": sorted([g.kind, g.name, round(g.memory_bytes / GB)] for g in self.gpus)}  # fmt: skip
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Benchmark:
    """One probe generation (PLT-08): speeds in tokens per second on the named model."""

    model: str
    prompt_tokens_s: float
    generation_tokens_s: float
    seconds: float


@dataclass(frozen=True)
class Shortfall:
    """A minimum-laptop check that failed, with the figures the notice names."""

    check: Literal["ram", "cpu", "disk", "speed"]
    have: float | str
    need: float | str
    unit: str = ""


@dataclass(frozen=True)
class ClassChoice:
    hardware_class: str
    provisional: bool  # no benchmark yet: predicted from the hardware alone
    shortfalls: tuple[Shortfall, ...] = ()  # why the machine is below the minimum laptop (PLT-29)
    reasons: tuple[str, ...] = field(default_factory=tuple)  # which rule placed it, for the health panel (OBS-15)


# --- the CPU's release year, from its model name -----------------------------------------------------------------

# Intel Core generations and AMD Ryzen series by year of first laptops; a model this table cannot place has no year
# and the benchmark alone decides.
_INTEL_GEN_YEAR = {2: 2011, 3: 2012, 4: 2013, 5: 2015, 6: 2015, 7: 2016, 8: 2018, 9: 2019, 10: 2019, 11: 2020,
                   12: 2022, 13: 2023, 14: 2024}  # fmt: skip
_RYZEN_SERIES_YEAR = {1: 2017, 2: 2018, 3: 2019, 4: 2020, 5: 2021, 6: 2022, 7: 2023, 8: 2024, 9: 2024}


def cpu_year(model: str, arch: str = "x86_64") -> int | None:
    if re.search(r"\bApple M\d", model):
        return 2020 if re.search(r"\bApple M1\b", model) else 2022
    if re.search(r"Core\(TM\) Ultra|Core Ultra", model):
        return 2023
    intel = re.search(r"\bi[3579]-(\d{4,5})", model)
    if intel:
        digits = intel.group(1)
        # i5-8250U is 8th generation; i5-10400 and the laptop i7-1255U (four digits from a leading 1) are 10th+.
        generation = int(digits[:2]) if len(digits) == 5 or digits[0] == "1" else int(digits[0])
        return _INTEL_GEN_YEAR.get(generation)
    if re.search(r"Ryzen AI", model):
        return 2024
    ryzen = re.search(r"Ryzen [3579] (?:PRO )?(\d)\d{3}", model)
    if ryzen:
        return _RYZEN_SERIES_YEAR.get(int(ryzen.group(1)))
    return None


# --- the hardware class ------------------------------------------------------------------------------------------


def minimum_shortfalls(probe: Probe, settings: Settings) -> list[Shortfall]:
    """The PLT-29 checks: memory, a 64-bit CPU from the minimum year with SSE4.2 (x86) or arm64, and free disk."""
    short: list[Shortfall] = []
    ram_gb = probe.ram_bytes / GB
    # Installed memory reads a little under its label once firmware and graphics reserve some.
    if ram_gb + settings.ram_report_slack_gb < settings.minimum_ram_gb:
        short.append(Shortfall("ram", round(ram_gb), settings.minimum_ram_gb, "GB"))
    year = probe.cpu_year
    if probe.arch not in ("x86_64", "arm64"):
        short.append(Shortfall("cpu", probe.arch, "64-bit"))
    elif probe.arch == "x86_64" and "sse4_2" not in probe.cpu_features:
        short.append(Shortfall("cpu", "no SSE4.2", "SSE4.2"))
    elif year is not None and year < settings.minimum_cpu_year:
        short.append(Shortfall("cpu", year, settings.minimum_cpu_year, "year"))
    disk_gb = probe.free_disk_bytes / GB
    if disk_gb < settings.minimum_free_disk_gb:
        short.append(Shortfall("disk", round(disk_gb, 1), settings.minimum_free_disk_gb, "GB"))
    return short


def high_memory_ok(probe: Probe, settings: Settings) -> bool:
    """Memory for the high model: unified memory on Apple silicon, or a discrete GPU, with the class's minimum."""
    gpu = probe.discrete_gpu
    if probe.apple_silicon:
        return probe.ram_bytes / GB + settings.ram_report_slack_gb >= settings.high_class_min_memory_gb
    return gpu is not None and probe.ram_bytes / GB + settings.ram_report_slack_gb >= settings.high_class_min_memory_gb


def choose_class(probe: Probe, benchmark: Benchmark | None, settings: Settings) -> ClassChoice:
    """The highest class whose entry rule in TABLE PLT-08a holds; content-only when none does."""
    short = minimum_shortfalls(probe, settings)
    if short:
        return ClassChoice("content_only", benchmark is None, tuple(short), ("below the minimum laptop",))
    ram_gb = probe.ram_bytes / GB + settings.ram_report_slack_gb
    if benchmark is None:
        if high_memory_ok(probe, settings):
            return ClassChoice("high", True, reasons=("predicted: Apple silicon or a discrete GPU",))
        if ram_gb >= settings.mid_class_min_ram_gb:
            return ClassChoice("mid", True, reasons=("predicted: memory for the mid model",))
        return ClassChoice("low", True, reasons=("predicted: the minimum laptop",))
    speed, reading = benchmark.generation_tokens_s, benchmark.prompt_tokens_s
    # D128: the low class reads and writes fast enough after a realistic prompt; a machine that could not finish the
    # benchmark in its time limit measured nothing and is below both.
    slow = [Shortfall("speed", round(have, 1), need, unit)
            for have, need, unit in ((reading, settings.low_class_min_prompt_tokens_s, "tokens/s reading"),
                                     (speed, settings.low_class_min_tokens_s, "tokens/s writing"))
            if have < need]  # fmt: skip
    if slow:
        return ClassChoice("content_only", False, tuple(slow),
                           tuple(f"{s.unit.split()[1]} {s.have} tokens/s is under {s.need}" for s in slow))  # fmt: skip
    if speed >= settings.high_class_min_tokens_s and high_memory_ok(probe, settings):
        return ClassChoice("high", False, reasons=(f"generation {speed:.1f} tokens/s with memory for the high model",))
    if speed >= settings.mid_class_min_tokens_s and ram_gb >= settings.mid_class_min_ram_gb:
        return ClassChoice("mid", False, reasons=(f"generation {speed:.1f} tokens/s",))
    return ClassChoice("low", False, reasons=(f"generation {speed:.1f} tokens/s",))


# A fixed probe text of about 1,200 tokens, read uncached: the same work on every machine, so speeds compare, at the
# prompt length a tutor turn has (D128). On a CPU, reading and writing after a long prompt are much slower than after
# a short one (on the low reference VM writing halved by 2,500 tokens), and that is the speed a student waits for.
BENCHMARK_PROMPT = (
    "A trolley starts from rest and accelerates uniformly along a straight track. After four seconds it is moving "
    "at eight metres per second. A student wants to know the acceleration, the distance covered in those four "
    "seconds, and how far the trolley would travel in the next two seconds if the acceleration stayed the same. "
) * 18 + "Explain each step briefly."
BENCHMARK_TOKENS = 64


async def run_benchmark(provider: Any, model: str, settings: Settings) -> Benchmark:
    """The PLT-08 probe generation on `model` (the smallest installed one), bounded by `benchmark_max_s`. A machine
    that cannot finish it in that time measures no speed (D128: below the low class)."""
    try:
        timed = await provider.benchmark(model, BENCHMARK_PROMPT, BENCHMARK_TOKENS, settings.benchmark_max_s)
    except ProviderError as error:
        if error.reason != "timeout":
            raise
        return Benchmark(model, 0.0, 0.0, settings.benchmark_max_s)
    return Benchmark(model, timed["prompt_tokens_s"], timed["generation_tokens_s"], timed["seconds"])


# --- probing this machine ----------------------------------------------------------------------------------------


def _run(*args: str) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def probe_machine(data_dir: Path) -> Probe:
    """This machine, read without the network and without elevated rights."""
    system = platform.system()
    free = shutil.disk_usage(data_dir if data_dir.exists() else Path.home()).free
    cores = os.cpu_count() or 1
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    if system == "Darwin":
        return _probe_macos(arch, cores, free)
    if system == "Linux":
        return _probe_linux(arch, cores, free)
    return _probe_windows(arch, cores, free)


def _probe_macos(arch: str, cores: int, free: int) -> Probe:
    ram = int(_run("sysctl", "-n", "hw.memsize") or 0)
    model = _run("sysctl", "-n", "machdep.cpu.brand_string")
    if _run("sysctl", "-n", "hw.optional.arm64") == "1":
        arch = "arm64"  # a translated (Rosetta) process still reports the real machine here
    features = {f.lower().replace(".", "_") for f in (_run("sysctl", "-n", "machdep.cpu.features") + " "
                + _run("sysctl", "-n", "machdep.cpu.leaf7_features")).split()}  # fmt: skip
    gpus: tuple[Gpu, ...] = ()
    if arch == "arm64":
        gpus = (Gpu("apple", model, ram),)
    else:
        try:
            shown = json.loads(_run("system_profiler", "SPDisplaysDataType", "-json") or "{}")
        except ValueError:
            shown = {}
        found = []
        for card in shown.get("SPDisplaysDataType", []):
            vram = re.search(r"(\d+)\s*(GB|MB)", str(card.get("spdisplays_vram") or card.get("_spdisplays_vram") or ""))
            size = int(vram.group(1)) * (GB if vram.group(2) == "GB" else 1024**2) if vram else 0
            kind: GpuKind = "integrated" if "Intel" in str(card.get("sppci_model")) else "discrete"
            found.append(Gpu(kind, str(card.get("sppci_model") or "GPU"), size))
        gpus = tuple(found)
    page = int(_run("sysctl", "-n", "hw.pagesize") or 4096)
    stat = _run("vm_stat")
    pages = sum(int(n) for name, n in re.findall(r"Pages (free|inactive|speculative):\s+(\d+)", stat))
    available = pages * page if stat else None
    return Probe("macos", arch, ram, cores, model, frozenset(features), free, gpus, available)


def _probe_linux(arch: str, cores: int, free: int) -> Probe:
    meminfo = Path("/proc/meminfo").read_text() if Path("/proc/meminfo").exists() else ""
    total = re.search(r"MemTotal:\s+(\d+) kB", meminfo)
    ram = int(total.group(1)) * 1024 if total else 0
    free_now = re.search(r"MemAvailable:\s+(\d+) kB", meminfo)
    cpuinfo = Path("/proc/cpuinfo").read_text() if Path("/proc/cpuinfo").exists() else ""
    model = re.search(r"model name\s*:\s*(.+)", cpuinfo) or re.search(r"Model\s*:\s*(.+)", cpuinfo)
    flags = re.search(r"(?:flags|Features)\s*:\s*(.+)", cpuinfo)
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        vendor = (card / "device" / "vendor").read_text().strip() if (card / "device" / "vendor").exists() else ""
        vram_file = card / "device" / "mem_info_vram_total"
        vram = int(vram_file.read_text()) if vram_file.exists() else 0
        if vendor == "0x10de" or vram >= GB:  # NVIDIA, or an AMD card with its own memory
            gpus.append(Gpu("discrete", vendor, vram))
        elif vendor:
            gpus.append(Gpu("integrated", vendor, 0))
    return Probe("linux", arch, ram, cores, model.group(1).strip() if model else "", frozenset(
        flags.group(1).split() if flags else ()), free, tuple(gpus),
        int(free_now.group(1)) * 1024 if free_now else None)  # fmt: skip


def _probe_windows(arch: str, cores: int, free: int) -> Probe:  # pragma: no cover - Windows only
    import ctypes

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                    ("page_avail", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                    ("virtual_avail", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]  # fmt: skip

    status = Status()
    status.length = ctypes.sizeof(Status)
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel.GlobalMemoryStatusEx(ctypes.byref(status))
    present = {"sse4_2": 38, "avx": 39, "avx2": 40, "avx512f": 41}
    features = {name for name, code in present.items() if kernel.IsProcessorFeaturePresent(code)}
    if "avx2" in features:
        features.add("fma")  # every AVX2 laptop CPU also has FMA3
    model = platform.processor()
    shown = _run("powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json")  # fmt: skip
    try:
        cards = json.loads(shown) if shown else []
    except ValueError:
        cards = []
    cards = cards if isinstance(cards, list) else [cards]
    gpus = tuple(Gpu("discrete" if re.search(r"NVIDIA|Radeon RX|Arc A", str(c.get("Name"))) else "integrated",
                     str(c.get("Name")), int(c.get("AdapterRAM") or 0)) for c in cards)  # fmt: skip
    return Probe("windows", arch, int(status.total), cores, model, frozenset(features), free, gpus, int(status.avail))

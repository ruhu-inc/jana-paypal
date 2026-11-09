"""Provider-key scanner (SEC-20): the repository's tracked files, its history and built artefacts.

`python -m cloud_gateway.secret_scan [--repo DIR] [ARTEFACT ...]` exits 1 on a finding. Findings name the pattern
and where it was found, with the value masked, so the scanner never prints a key it finds.

Values that say they are fixtures (they contain "canary", "test", "example", "dummy", "fake" or "placeholder") are
not keys and are ignored; a real key never contains those words.

History is never rewritten, so a fixture committed without one of those words stays in it. `HISTORY_FIXTURES` lists
each such value by its sha256 (the list never holds the value), with the commit that brought it in and why it is not a
key. It applies to the history scan alone: the same value in a tracked file or an artefact is still a finding."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import re
import subprocess
import sys
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Each pattern starts with a literal, so a search over a large history stays fast. The header pattern runs over a
# lower-cased copy (same offsets); "not preceded by a letter or digit" is checked in code.
PATTERNS: dict[str, re.Pattern[bytes]] = {
    "google_api_key": re.compile(rb"AIza[0-9A-Za-z_\-]{35}"),
    "anthropic_key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{8,}"),
    "openai_key": re.compile(rb"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    "nvidia_api_key": re.compile(rb"nvapi-[A-Za-z0-9_\-]{20,}"),
    # Meta Model API keys look like LLM_<numeric id>_<secret> (dev.meta.ai/docs/authentication).
    "meta_api_key": re.compile(rb"LLM_[0-9]{4,}_[A-Za-z0-9_\-]{16,}"),
    # A web-search key from an earlier build may still be on a developer's machine; catching it costs nothing.
    "legacy_search_key": re.compile(rb"tvly-[A-Za-z0-9_\-]{20,}"),
    "goog_api_key_header": re.compile(rb"x-goog-api-key[\"']?\s*[:=]\s*[\"']?([a-z0-9_\-]{20,})"),
}
_BOUNDED = frozenset({"anthropic_key", "openai_key", "nvidia_api_key", "meta_api_key", "legacy_search_key"})
_HEADER = "goog_api_key_header"
FIXTURE_WORDS = (b"canary", b"test", b"example", b"dummy", b"fake", b"placeholder")
HISTORY_FIXTURES: dict[str, str] = {
    "e200accd6c6c2e371fa329c7ce98f4d2d6f3f71266a15b3b4e719256ea7a8e83": (
        "commit 9ebc55f3 (M20.f): the support-bundle test's made-up api_key, seeded to prove the bundle strips it "
        "(OBS-18); never a provider key. The test now seeds a fixture-marked value."
    ),
    "735f7219f1e1d1e4655429d75c20e41e96005311b489fe2fb470bd583298dd75": (
        "commit 7a92235d (W8): test_w8_memory_online.py's made-up key in a note, seeded to prove the egress scrub "
        "withholds it (SPEC §19.6); never a provider key. The test now seeds a fixture-marked value."
    ),
}


@dataclass(frozen=True)
class Finding:
    pattern: str
    where: str
    masked: str

    def __str__(self) -> str:
        return f"{self.where}: {self.pattern} {self.masked}"


def _mask(value: bytes) -> str:
    return value[:6].decode(errors="replace") + "…" + f"({len(value)} chars)"


def _matches(data: bytes) -> Iterator[tuple[str, int, bytes]]:
    lowered = data.lower()
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(lowered if name == _HEADER else data):
            start, end = match.span(match.lastindex or 0)
            value = data[start:end]
            if name in _BOUNDED and data[max(0, match.start() - 1) : match.start()].isalnum():
                continue
            if name == "openai_key" and value.startswith(b"sk-ant-"):
                continue  # reported as anthropic_key
            if any(word in value.lower() for word in FIXTURE_WORDS):
                continue
            yield name, match.start(), value


def scan_bytes(data: bytes, where: str) -> list[Finding]:
    return [Finding(name, where, _mask(value)) for name, _, value in _matches(data)]


def scan_files(paths: Iterable[tuple[str, Path]]) -> list[Finding]:
    found = []
    for name, path in paths:
        if path.is_file() and not path.is_symlink():
            found += scan_bytes(path.read_bytes(), name)
    return found


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout


def scan_tracked(repo: Path) -> list[Finding]:
    names = [n.decode() for n in _git(repo, "ls-files", "-z").split(b"\0") if n]
    return scan_files((name, repo / name) for name in names)


def scan_history(repo: Path) -> list[Finding]:
    """Every patch reachable from HEAD, each finding attributed to its commit."""
    log = _git(repo, "log", "-p", "--no-color", "--no-ext-diff", "HEAD")
    starts = [(m.start(), m.group(1).decode()[:12]) for m in re.finditer(rb"^commit ([0-9a-f]{40})", log, re.M)]
    offsets = [offset for offset, _ in starts]
    found = []
    for name, at, value in _matches(log):
        if hashlib.sha256(value).hexdigest() in HISTORY_FIXTURES:
            continue
        index = bisect.bisect_right(offsets, at) - 1
        found.append(Finding(name, f"commit {starts[index][1] if index >= 0 else 'HEAD'}", _mask(value)))
    return found


def scan_artefact(path: Path) -> list[Finding]:
    """A wheel or other zip archive member by member, a directory file by file, or a single file."""
    if path.is_dir():
        return scan_files((str(p.relative_to(path)), p) for p in sorted(path.rglob("*")))
    if zipfile.is_zipfile(path):
        found = []
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                found += scan_bytes(archive.read(member), f"{path.name}!{member}")
        return found
    return scan_bytes(path.read_bytes(), path.name)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cloud_gateway.secret_scan")
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("artefacts", nargs="*", type=Path)
    args = p.parse_args(argv)
    findings = scan_tracked(args.repo) + scan_history(args.repo)
    for artefact in args.artefacts:
        findings += scan_artefact(artefact)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

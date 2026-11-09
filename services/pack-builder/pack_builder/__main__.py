"""Command line: python -m pack_builder build <source dir> <out dir>."""

from __future__ import annotations

import sys
from pathlib import Path

from pack_builder.build import BuildError, build


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "build":
        print("usage: python -m pack_builder build <source dir> <out dir>", file=sys.stderr)
        return 2
    try:
        built = build(Path(argv[1]), Path(argv[2]))
    except BuildError as error:
        print(f"build failed: {error}", file=sys.stderr)
        return 1
    print(f"built {built.pack_path} ({built.manifest['pack_id']} {built.manifest['version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

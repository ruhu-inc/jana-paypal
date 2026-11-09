"""Defence in depth after fixed codecs/native bindings preload; the OS sandbox remains the boundary."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy.linalg
import numpy.ma
from PIL import Image, JpegImagePlugin, PngImagePlugin, TiffImagePlugin, WebPImagePlugin

from perception_worker.ocr import assets

# Imports above are deliberate preloads, never names taken from image data.
PRELOADED = (JpegImagePlugin, PngImagePlugin, TiffImagePlugin, WebPImagePlugin, numpy.linalg, numpy.ma)
FORBIDDEN = {"socket", "ssl", "http", "urllib", "subprocess", "ctypes", "multiprocessing", "pickle", "marshal"}


class RefuseImports:
    def find_spec(self, fullname: str, *_: Any) -> None:
        if fullname.partition(".")[0] in FORBIDDEN:
            raise PermissionError("policy_denied")
        return None


def install() -> None:
    Image.preinit()
    Image.init()
    folder = assets()
    reads = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), Path(__file__).parent.resolve()]
    if folder:
        reads.append(Path(folder).resolve())
    # No work after this point can import a client, spawn a process, evaluate code or load a native library.
    for name in list(sys.modules):
        if name.partition(".")[0] in FORBIDDEN:
            del sys.modules[name]
    sys.meta_path.insert(0, RefuseImports())

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in (
            "exec",
            "compile",
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "ctypes.dlopen",
            "ctypes.dlsym",
        ) or event.startswith("socket."):
            raise PermissionError("policy_denied")
        if event == "open" and args:
            target = args[0]
            if isinstance(target, str | bytes):
                path = Path(target.decode() if isinstance(target, bytes) else target).resolve()
                if not any(path.is_relative_to(root) for root in reads):
                    raise PermissionError("policy_denied")

    sys.addaudithook(audit)

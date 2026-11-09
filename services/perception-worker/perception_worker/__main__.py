"""One bounded image per process, JSON over stdio. No image or transcript is logged."""

from __future__ import annotations

import base64
import json
import sys

from perception_worker.image import ImageRejected, preprocess
from perception_worker.policy import install


def main() -> None:
    # Native decoding gets a bounded process lifetime and address space (parent also enforces a deadline).
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (18, 20))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    install()
    raw = sys.stdin.buffer.readline(30_000_000)
    if len(raw) >= 30_000_000 or not raw.endswith(b"\n"):
        result = {"error": "image_too_large"}
    else:
        try:
            request = json.loads(raw)
            result = preprocess(
                base64.b64decode(request["data"], validate=True),
                corners=request.get("corners"),
                rotation=request.get("rotation", 0),
                preview_only=request.get("preview_only") is True,
            )
        except ImageRejected as error:
            result = {"error": error.code}
        except Exception:
            result = {"error": "photo_reading_unavailable"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()

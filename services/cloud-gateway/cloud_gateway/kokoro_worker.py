"""Kokoro-82M worker for cloud voice (SPEC §6.4): run by `KokoroSynthesizer` under the speech runtime's interpreter.

    <speech python> -m cloud_gateway.kokoro_worker MODEL_DIR SPEAKER THREADS FIRST_CLAUSE_WORDS

Loads the speech pack's Kokoro (local files only, through the core's `KokoroEngine`: no espeak, its own spelling
fallback) and reads requests from stdin, one JSON line each: {"id": n, "text": "..."}. Replies on stdout as frames:
a 4-byte request id and a 4-byte signed length (big-endian), then that many bytes of 24 kHz mono PCM16; length -1 ends
a request, -2 says it failed. Request 0 with length -1 says the model is loaded. The text is read in the order given,
split once at the first clause of its first sentence so the first audio comes sooner; nothing is logged or written.
Imports only the standard library and `agent_core` (the speech runtime has no gateway dependencies)."""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path


def frame(out: object, request: int, size: int, payload: bytes = b"") -> None:
    out.write(struct.pack(">Ii", request, size) + payload)  # type: ignore[attr-defined]
    out.flush()  # type: ignore[attr-defined]


def main(argv: list[str]) -> int:
    model_dir, speaker, threads, first_clause = Path(argv[0]), argv[1], int(argv[2]), int(argv[3])
    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # library prints never reach the frame stream
    from agent_core.net.voice_speech import KokoroEngine, speech_units

    engine = KokoroEngine(model_dir, speaker, threads)
    for _ in engine.stream("Ready."):  # the first call loads the pipeline's lexicons: pay for it before a student
        pass
    frame(out, 0, -1)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            ident, text = int(request["id"]), str(request["text"])
        except (ValueError, KeyError, TypeError):
            continue
        try:
            for unit in speech_units(text, first_clause):
                if unit.strip():
                    for pcm in engine.stream(unit.strip()):
                        frame(out, ident, len(pcm), pcm)
            frame(out, ident, -1)
        except Exception:
            frame(out, ident, -2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

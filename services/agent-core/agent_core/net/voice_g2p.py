# ruff: noqa: RUF001 -- IPA phoneme symbols are intended
"""Pronunciations for words outside Kokoro's lexicon (D95: no espeak-ng, so misaki has no fallback of its own).

Without a fallback, Kokoro's pipeline silently drops any word its lexicon lacks: the tutor's own name ("Jana"), a
student's name, an unusual term. This module gives misaki a fallback in two steps:

1. Fixed pronunciations for the tutor characters' names (D103), so the tutor always says its own name the same way.
2. A spelling-to-sound model for everything else: the small GRU encoder-decoder trained on the CMU Pronouncing
   Dictionary by Park and Kim's g2p_en (Apache-2.0). Only its weights (`checkpoint20.npz`) ship in the speech pack;
   the decoding below is this module's own numpy, so no g2p_en runtime (and none of its GPL-licensed dependencies) is
   loaded. Its ARPAbet output is mapped to misaki's American phoneme set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

# D103: the six tutor characters (D125: avatar.ray is shown as Remy). The pronunciation of "Jana" is JAH-nuh.
NAMES = {
    "jana": "ʤˈɑnə",
    "orbit": "ˈɔɹbət",
    "prism": "pɹˈɪzəm",
    "kite": "kˈIt",
    "pebble": "pˈɛbəl",
    "ray": "ɹˈA",
    "remy": "ɹˈɛmi",
}

GRAPHEMES = ["<pad>", "<unk>", "</s>", *"abcdefghijklmnopqrstuvwxyz"]
# The checkpoint's output vocabulary, in its training order.
PHONEMES = ["<pad>", "<unk>", "<s>", "</s>", *(
    "AA0 AA1 AA2 AE0 AE1 AE2 AH0 AH1 AH2 AO0 AO1 AO2 AW0 AW1 AW2 AY0 AY1 AY2 B CH D DH EH0 EH1 EH2 ER0 ER1 ER2 EY0 "
    "EY1 EY2 F G HH IH0 IH1 IH2 IY0 IY1 IY2 JH K L M N NG OW0 OW1 OW2 OY0 OY1 OY2 P R S SH T TH UH0 UH1 UH2 UW UW0 "
    "UW1 UW2 V W Y Z ZH"
).split()]  # fmt: skip
VOWELS = {"AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "W", "AY": "I", "EH": "ɛ", "ER": "ɜɹ", "EY": "A", "IH": "ɪ",
          "IY": "i", "OW": "O", "OY": "Y", "UH": "ʊ", "UW": "u"}  # fmt: skip
CONSONANTS = {"B": "b", "CH": "ʧ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ", "HH": "h", "JH": "ʤ", "K": "k", "L": "l",
              "M": "m", "N": "n", "NG": "ŋ", "P": "p", "R": "ɹ", "S": "s", "SH": "ʃ", "T": "t", "TH": "θ", "V": "v",
              "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ"}  # fmt: skip
MAX_WORD = 32  # longer tokens (codes, URLs) are not words to pronounce


def to_misaki(arpabet: list[str]) -> str:
    out = []
    for symbol in arpabet:
        base, stress = symbol.rstrip("012"), symbol[len(symbol.rstrip("012")) :]
        if base in CONSONANTS:
            out.append(CONSONANTS[base])
        elif base in VOWELS:
            vowel = VOWELS[base]
            if stress == "0" and base in ("AH", "ER"):
                vowel = "ə" if base == "AH" else "əɹ"
            out.append({"1": "ˈ", "2": "ˌ"}.get(stress, "") + vowel)
    return "".join(out)


class SpellingModel:
    """Greedy decoding of the g2p_en GRU weights: lowercase letters in, ARPAbet out."""

    def __init__(self, weights: Path) -> None:
        with np.load(weights) as data:
            self.w = {name: data[name].astype(np.float32) for name in data.files}

    @staticmethod
    def _cell(x: Any, h: Any, w_ih: Any, w_hh: Any, b_ih: Any, b_hh: Any) -> Any:
        gi, gh = x @ w_ih.T + b_ih, h @ w_hh.T + b_hh
        third = gi.shape[-1] // 3
        r = 1 / (1 + np.exp(-(gi[:, :third] + gh[:, :third])))
        z = 1 / (1 + np.exp(-(gi[:, third : 2 * third] + gh[:, third : 2 * third])))
        n = np.tanh(gi[:, 2 * third :] + r * gh[:, 2 * third :])
        return (1 - z) * n + z * h

    def arpabet(self, word: str) -> list[str]:
        w = self.w
        h = np.zeros((1, w["enc_w_hh"].shape[1]), np.float32)
        for char in [*word, "</s>"]:
            index = GRAPHEMES.index(char) if char in GRAPHEMES else 1
            h = self._cell(w["enc_emb"][[index]], h, w["enc_w_ih"], w["enc_w_hh"], w["enc_b_ih"], w["enc_b_hh"])
        token, out = 2, []  # 2: <s>
        for _ in range(20):
            h = self._cell(w["dec_emb"][[token]], h, w["dec_w_ih"], w["dec_w_hh"], w["dec_b_ih"], w["dec_b_hh"])
            token = int(np.argmax(h @ w["fc_w"].T + w["fc_b"]))
            if token == 3:  # </s>
                break
            out.append(PHONEMES[token])
        return out


class Fallback:
    """misaki's `fallback(token) -> (phonemes, rating)` for words its lexicon lacks; None leaves the word unspoken."""

    def __init__(self, weights: Path | None) -> None:
        self.model = SpellingModel(weights) if weights is not None and weights.is_file() else None

    def __call__(self, token: Any) -> tuple[str | None, int | None]:
        word = str(getattr(token, "text", token)).strip()
        key = word.lower().strip("'’")
        if key in NAMES:
            return NAMES[key], 4
        letters = "".join(c for c in key if c.isalpha())
        if self.model is None or not letters.isascii() or not 0 < len(letters) <= MAX_WORD:
            return None, None
        phonemes = to_misaki(self.model.arpabet(letters))
        return (phonemes or None), (1 if phonemes else None)

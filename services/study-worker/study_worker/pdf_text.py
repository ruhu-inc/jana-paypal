"""Known Adobe Symbol font encodings, without inferring missing formula structure."""

# Adobe's legacy private-use delimiter pieces have standard Unicode equivalents.
# Keep the pieces (and their line breaks), rather than guessing a matrix or cases expression.
PDF_SYMBOLS = str.maketrans(
    dict(
        zip(
            "\uf8eb\uf8ec\uf8ed\uf8ee\uf8ef\uf8f0\uf8f1\uf8f2\uf8f3\uf8f4\uf8f6\uf8f7\uf8f8\uf8f9\uf8fa\uf8fb\uf8fc\uf8fd\uf8fe",
            "⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪⎞⎟⎠⎤⎥⎦⎫⎬⎭",
            strict=True,
        )
    )
)


def normalize_pdf_text(text: str) -> str:
    """No NFKC: it would flatten valid superscripts and change mathematical meaning."""
    return text.translate(PDF_SYMBOLS)

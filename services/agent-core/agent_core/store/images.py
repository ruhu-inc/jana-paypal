"""Photos made safe before they are kept or read by a model (SEC-05).

A photo is untrusted input. Its size is checked before anything is decoded, and its pixel dimensions are read from
the header and checked before the pixels are, so a small file that would expand to gigabytes (a decompression bomb)
is refused without using the memory. Only JPEG and PNG are read. The photo is turned upright from its orientation
tag, downscaled, and re-encoded as a new JPEG that carries no metadata at all: no EXIF (and so no GPS location,
device or time), no XMP and no colour profile. Only the re-encoded bytes are stored or sent on.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from agent_core.config import Settings

FORMATS = ("JPEG", "PNG")


class PhotoRejectedError(ValueError):
    """The photo cannot be read safely; `code` is the API error (`image_too_large` or `unsupported_format`)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class SafePhoto:
    jpeg: bytes
    width: int
    height: int


def make_safe(data: bytes, settings: Settings) -> SafePhoto:
    """The photo as a downscaled JPEG without metadata, or `PhotoRejectedError`."""
    if len(data) > settings.photo_max_mb * 1024 * 1024:
        raise PhotoRejectedError("image_too_large", f"{len(data)} bytes")
    limit = settings.photo_max_side_px
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            opened = Image.open(io.BytesIO(data), formats=FORMATS)  # reads the header only
            width, height = opened.size
            if width > limit or height > limit or width < 1 or height < 1:
                raise PhotoRejectedError("image_too_large", f"{width}x{height} px")
            # Decodes; turned upright before the orientation tag is dropped.
            image: Image.Image = ImageOps.exif_transpose(opened).convert("RGB")
    except PhotoRejectedError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise PhotoRejectedError("image_too_large", str(error)) from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise PhotoRejectedError("unsupported_format", type(error).__name__) from error
    side = settings.photo_downscale_side_px
    image.thumbnail((side, side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    # A fresh image object written without exif=, icc_profile= or xmp=: Pillow writes no metadata segment.
    clean = Image.new("RGB", image.size)
    clean.paste(image)
    clean.save(out, "JPEG", quality=settings.photo_jpeg_quality, optimize=True)
    return SafePhoto(out.getvalue(), clean.width, clean.height)

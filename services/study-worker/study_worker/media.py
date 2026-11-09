"""Server-only media decoding inside the network-denied worker; all source, PCM and pixel bytes stay in memory.

A recording's audio is decoded in 60-second segments (STU-22). A video file (SPEC §20.3, JM-D34) adds stream info
(`has_video`, `has_audio`, `width`, `height`), a container with no audio stream (frames only), a scan of keyframes
for scene changes, and stills decoded at chosen moments and read as text through the photo privacy pass and OCR:
no pixel leaves this process, a frame the pass refuses returns `screened`."""

from __future__ import annotations

import base64
import io
import math
import wave
from typing import Any

from study_worker import DocumentError

FORMATS = {"wav", "mp3", "flac", "aac", "ogg", "mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi"}
RATE = 16000
FRAME_GAP_MS = 30_000  # at most one scene frame per 30 s (SPEC §20.3)
FRAMES_MAX = 60  # and 60 per file
FRAMES_PER_CALL = 10  # batches, within the worker's CPU limit
KEYFRAMES_SCANNED_MAX = 2_000
SCENE_THRESHOLD = 12.0  # mean absolute difference of 32x18 colour thumbnails, out of 255
FRAME_WIDTH_MAX = 1280
OCR_CONFIDENCE = 0.85


def _open(data: bytes) -> Any:
    import av  # Installed only on the gateway, not a desktop dependency.

    container = av.open(io.BytesIO(data), mode="r", options={"protocol_whitelist": "pipe", "enable_drefs": "0"})
    if not set(container.format.name.split(",")) & FORMATS:
        container.close()
        raise DocumentError("unsupported_format")
    return container


def _duration_s(container: Any, stream: Any) -> float | None:
    import av

    if stream is not None and stream.duration is not None:
        return float(stream.duration * stream.time_base)
    if container.duration is not None:
        return container.duration / av.time_base
    return None


def decode(
    data: bytes, start_ms: int | None, max_minutes: int, verified_duration_ms: int | None = None
) -> dict[str, Any]:
    """Stream info (`start_ms` None): duration, whether the container has audio and video streams, the picture's
    size; or a 60-second mono WAV segment of the audio from `start_ms` (a container with no audio stream has no
    segments: `unreadable_source`)."""
    import av

    try:
        with _open(data) as container:
            audio = container.streams.audio[0] if container.streams.audio else None
            video = container.streams.video[0] if container.streams.video else None
            if audio is None and video is None:
                raise DocumentError("unsupported_format")
            stream = audio if audio is not None else video
            stream.codec_context.thread_type = "SLICE"
            stream.codec_context.thread_count = 1
            duration = _duration_s(container, stream)
            if duration is None or not math.isfinite(duration) or duration <= 0:
                raise DocumentError("unreadable_source")
            duration_ms = math.ceil(duration * 1000)
            origin = float((stream.start_time or 0) * stream.time_base)
            if start_ms is None:
                # Inspect packet timestamps too: a forged short header must not silently truncate the source.
                for count, packet in enumerate(container.demux(stream)):
                    if count > 2_000_000:
                        raise DocumentError("source_too_large")
                    if packet.pts is not None:
                        duration_ms = max(
                            duration_ms,
                            math.ceil(
                                (float((packet.pts + (packet.duration or 0)) * stream.time_base) - origin) * 1000
                            ),
                        )
                    if duration_ms > max_minutes * 60000:
                        raise DocumentError("source_too_large")
            elif verified_duration_ms is not None:
                if type(verified_duration_ms) is not int or not 0 < verified_duration_ms <= max_minutes * 60000:
                    raise DocumentError("source_too_large")
                duration_ms = verified_duration_ms
            if duration_ms > max_minutes * 60000:
                raise DocumentError("source_too_large")
            if start_ms is None:
                return {
                    "duration_ms": duration_ms,
                    "has_audio": audio is not None,
                    "has_video": video is not None,
                    "width": int(video.codec_context.width) if video is not None else None,
                    "height": int(video.codec_context.height) if video is not None else None,
                }
            if audio is None:
                raise DocumentError("unreadable_source")  # frames only: nothing to transcribe
            if type(start_ms) is not int or start_ms < 0 or start_ms >= duration_ms:
                raise DocumentError("invalid_request")
            end_ms = min(duration_ms, start_ms + 60000)
            container.seek(max(0, int((origin + start_ms / 1000 - 1) / audio.time_base)), stream=audio)
            resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
            pcm = bytearray()
            for frame in container.decode(audio):
                if frame.time is None:
                    raise DocumentError("unreadable_source")
                if (frame.time - origin) * 1000 >= end_ms:
                    break
                for sample in resampler.resample(frame):
                    if sample.pts is None:
                        raise DocumentError("unreadable_source")
                    at_ms = (float(sample.pts * sample.time_base) - origin) * 1000
                    first = max(0, round((start_ms - at_ms) * RATE / 1000))
                    last = min(sample.samples, round((end_ms - at_ms) * RATE / 1000))
                    if last > first:
                        pcm.extend(bytes(sample.planes[0])[first * 2 : last * 2])
                    if len(pcm) > 60000 * RATE * 2 // 1000:
                        raise DocumentError("source_too_large")
            if not pcm:
                raise DocumentError("unreadable_source")
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(RATE)
                wav.writeframes(pcm)
            return {
                "duration_ms": duration_ms,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "wav_base64": base64.b64encode(output.getvalue()).decode(),
            }
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("corrupt_source") from None


def _thumbnail(frame: Any) -> Any:
    image = frame.to_image().convert("RGB")
    image.thumbnail((32, 18))
    return image


def _difference(a: Any, b: Any) -> float:
    """The mean absolute difference of two thumbnails over their colour channels, out of 255."""
    from PIL import ImageChops, ImageStat

    if a.size != b.size:
        b = b.resize(a.size)
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return float(sum(means) / len(means))


def keyframe_times(
    data: bytes, max_minutes: int, gap_ms: int = FRAME_GAP_MS, limit: int = FRAMES_MAX
) -> list[dict[str, Any]]:
    """Scene changes between keyframes (SPEC §20.3): the first keyframe, then each keyframe whose greyscale
    thumbnail differs from the last kept one by more than `SCENE_THRESHOLD`, at most one per `gap_ms` and `limit`
    per file. Keyframes only (the CPU limit); the container's own chapter markers are not read (PyAV exposes none)."""
    try:
        with _open(data) as container:
            if not container.streams.video:
                return []
            video = container.streams.video[0]
            video.codec_context.thread_type = "SLICE"
            video.codec_context.thread_count = 1
            video.codec_context.skip_frame = "NONKEY"
            origin = float((video.start_time or 0) * video.time_base)
            kept: list[dict[str, Any]] = []
            last_image = None
            last_ms = -gap_ms
            scanned = 0
            for frame in container.decode(video):
                scanned += 1
                if scanned > KEYFRAMES_SCANNED_MAX or len(kept) >= limit:
                    break
                if frame.time is None:
                    continue
                at_ms = round((frame.time - origin) * 1000)
                if at_ms < 0 or at_ms > max_minutes * 60000:
                    continue
                image = _thumbnail(frame)
                if last_image is None:
                    kept.append({"at_ms": at_ms, "reason": "scene"})
                    last_image, last_ms = image, at_ms
                    continue
                if at_ms - last_ms < gap_ms:
                    continue
                if _difference(last_image, image) > SCENE_THRESHOLD:
                    kept.append({"at_ms": at_ms, "reason": "scene"})
                    last_image, last_ms = image, at_ms
            return kept
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("corrupt_source") from None


def _still(container: Any, video: Any, at_ms: int) -> bytes | None:
    """The keyframe nearest before `at_ms`, as PNG bytes no wider than `FRAME_WIDTH_MAX`."""
    origin = float((video.start_time or 0) * video.time_base)
    container.seek(max(0, int((origin + at_ms / 1000) / video.time_base)), stream=video, backward=True, any_frame=False)
    for frame in container.decode(video):
        image = frame.to_image().convert("RGB")
        if image.width > FRAME_WIDTH_MAX:
            image = image.resize((FRAME_WIDTH_MAX, max(1, image.height * FRAME_WIDTH_MAX // image.width)))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    return None


def frames_text(data: bytes, moments: list[int]) -> list[dict[str, Any]]:
    """Each moment's still read as text (SPEC §20.3, JM-D34): the photo privacy pass first (a refused frame is
    `screened`, no text), then OCR; only lines the reader is sure of become text. Never more than
    `FRAMES_PER_CALL` per call; pixels never leave this function."""
    from perception_worker.image import preprocess  # type: ignore[import-untyped]

    if len(moments) > FRAMES_PER_CALL or any(type(m) is not int or m < 0 for m in moments):
        raise DocumentError("invalid_request")
    out: list[dict[str, Any]] = []
    try:
        with _open(data) as container:
            if not container.streams.video:
                raise DocumentError("unreadable_source")
            video = container.streams.video[0]
            video.codec_context.thread_type = "SLICE"
            video.codec_context.thread_count = 1
            video.codec_context.skip_frame = "NONKEY"
            for at_ms in moments:
                png = _still(container, video, at_ms)
                if png is None:
                    out.append({"at_ms": at_ms, "text": "", "screened": False})
                    continue
                reading = preprocess(png)
                if reading.get("refusal"):
                    out.append({"at_ms": at_ms, "text": None, "screened": True})
                    continue
                if reading.get("ocr_model_version") is None:
                    # No reader for the picture's writing: the frame stays unreadable; the transcript is unaffected.
                    out.append({"at_ms": at_ms, "text": "", "screened": False})
                    continue
                lines = reading.get("lines", [])
                text = (
                    "\n".join(line["text"] for line in lines)
                    if lines
                    and all(
                        line["confidence"] >= OCR_CONFIDENCE and not line.get("low_confidence_spans") for line in lines
                    )
                    else ""
                )
                out.append({"at_ms": at_ms, "text": text.strip(), "screened": False})
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("corrupt_source") from None
    return out

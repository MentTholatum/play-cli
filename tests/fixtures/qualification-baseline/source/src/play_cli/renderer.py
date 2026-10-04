from __future__ import annotations

import io
import struct
import sys
import wave
from array import array
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from play_cli.audio import PcmWavValidationError, validate_pcm16_mono_wav
from play_cli.score import ScheduledEvent
from play_cli.sound_pack import SoundPackFailure, VerifiedSoundPack, read_verified_sample

RENDERER_ID = "pcm-sampler"
RENDERER_VERSION = "2.0"
RELEASE_FADE_FRAMES = 160
MAX_RENDER_SECONDS = 600
MAX_RENDER_FRAMES = 28_800_000


@dataclass(frozen=True)
class WavMetadata:
    format: str
    sample_rate_hz: int
    channels: int
    frames: int


@dataclass(frozen=True)
class RenderedSampleMetadata:
    midi: int
    sha256: str


@dataclass(frozen=True)
class SoundPackMetadata:
    id: str
    version: str
    pack_hash: str
    samples: tuple[RenderedSampleMetadata, ...]


@dataclass(frozen=True)
class RenderedPerformance:
    wav_data: bytes
    wav: WavMetadata
    sound_pack: SoundPackMetadata


class RenderFailure(Exception):
    def __init__(self, error_id: str, message: str, diagnostic: str | None = None) -> None:
        super().__init__(diagnostic or message)
        self.error_id = error_id
        self.message = message


def _round_frame(value: Fraction) -> int:
    whole, remainder = divmod(value.numerator, value.denominator)
    return whole + int(remainder * 2 >= value.denominator)


def _event_frame(quarters: Fraction, tempo_bpm: Decimal, sample_rate: int) -> int:
    return _round_frame(quarters * sample_rate * 60 / Fraction(tempo_bpm))


def _bounded_frame_count(total_quarters: Fraction, tempo_bpm: Decimal, sample_rate: int) -> int:
    duration_seconds = total_quarters * 60 / Fraction(tempo_bpm)
    if duration_seconds > MAX_RENDER_SECONDS:
        raise RenderFailure(
            "renderer_duration_unsupported",
            "Performance duration exceeds the renderer limit.",
            f"Performance exceeds the {MAX_RENDER_SECONDS}-second renderer limit.",
        )
    total_frames = _round_frame(duration_seconds * sample_rate)
    if total_frames > MAX_RENDER_FRAMES:
        raise RenderFailure(
            "renderer_duration_unsupported",
            "Performance duration exceeds the renderer limit.",
            f"Performance exceeds the {MAX_RENDER_FRAMES}-frame renderer limit.",
        )
    return total_frames


def _pcm_frames(data: bytes, expected_sample_rate_hz: int) -> tuple[int, ...]:
    try:
        validated = validate_pcm16_mono_wav(
            data,
            expected_sample_rate_hz=expected_sample_rate_hz,
        )
        return struct.unpack(f"<{validated.frames}h", validated.frame_data)
    except (PcmWavValidationError, struct.error) as error:
        raise RenderFailure(
            "renderer_sample_incompatible",
            "Sound Pack sample is incompatible with the renderer.",
        ) from error


def _release_fade(samples: tuple[int, ...], frame_count: int) -> tuple[int, ...]:
    rendered = list(samples[:frame_count])
    if len(samples) <= frame_count or not rendered:
        return tuple(rendered)
    fade_frames = min(RELEASE_FADE_FRAMES, len(rendered))
    denominator = max(1, fade_frames - 1)
    for offset in range(fade_frames):
        index = len(rendered) - fade_frames + offset
        remaining = fade_frames - 1 - offset
        magnitude = (abs(rendered[index]) * remaining + denominator // 2) // denominator
        rendered[index] = magnitude if rendered[index] >= 0 else -magnitude
    return tuple(rendered)


def _read_back_wav(wav_data: bytes, expected_rate: int, expected_frames: int) -> None:
    try:
        validate_pcm16_mono_wav(
            wav_data,
            expected_sample_rate_hz=expected_rate,
            expected_frames=expected_frames,
        )
    except PcmWavValidationError as error:
        raise RenderFailure("render_failed", "Performance could not be rendered.") from error


def _render_pcm_schedule(
    events: tuple[ScheduledEvent, ...],
    tempo_bpm: Decimal,
    sound_pack: VerifiedSoundPack,
) -> RenderedPerformance:
    payloads = {}
    for event in events:
        if event.pitch is None or event.pitch.midi in payloads:
            continue
        try:
            payloads[event.pitch.midi] = read_verified_sample(sound_pack, event.pitch.midi)
        except SoundPackFailure as error:
            raise RenderFailure(error.error_id, error.message, error.diagnostic) from error

    sample_rates = {payload.sample.sample_rate_hz for payload in payloads.values()}
    if not sample_rates:
        sample_rates = {sample.sample_rate_hz for sample in sound_pack.samples}
    if len(sample_rates) != 1:
        raise RenderFailure(
            "renderer_sample_incompatible",
            "Sound Pack samples are incompatible with the renderer.",
            "Selected samples must use one sample rate.",
        )
    sample_rate = next(iter(sample_rates))
    total_quarters = max(event.onset + event.duration for event in events)
    total_frames = _bounded_frame_count(total_quarters, tempo_bpm, sample_rate)
    event_frames: list[tuple[ScheduledEvent, int, int]] = []
    for event in events:
        start = _event_frame(event.onset, tempo_bpm, sample_rate)
        end = _event_frame(event.onset + event.duration, tempo_bpm, sample_rate)
        if event.pitch is not None and end - start < RELEASE_FADE_FRAMES:
            raise RenderFailure(
                "renderer_timing_unrepresentable",
                "Score timing cannot be represented by the renderer.",
                f"A note requires at least {RELEASE_FADE_FRAMES} PCM frames.",
            )
        event_frames.append((event, start, end))
    rendered_frames = bytearray(total_frames * 2)

    for event, start, end in event_frames:
        if event.pitch is None:
            continue
        source_frames = _pcm_frames(payloads[event.pitch.midi].data, sample_rate)
        note_frames = _release_fade(source_frames, end - start)
        encoded = array("h", note_frames)
        if sys.byteorder != "little":
            encoded.byteswap()
        byte_start = start * 2
        rendered_frames[byte_start : byte_start + len(encoded) * 2] = encoded.tobytes()

    wav_buffer = io.BytesIO()
    try:
        with wave.open(wav_buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(rendered_frames)
    except (OSError, wave.Error) as error:
        raise RenderFailure("render_failed", "Performance could not be rendered.") from error
    wav_data = wav_buffer.getvalue()
    _read_back_wav(wav_data, sample_rate, total_frames)

    sample_metadata = tuple(
        RenderedSampleMetadata(midi=midi, sha256=payload.sample.sha256)
        for midi, payload in sorted(payloads.items())
    )
    return RenderedPerformance(
        wav_data=wav_data,
        wav=WavMetadata(
            format="PCM_S16LE",
            sample_rate_hz=sample_rate,
            channels=1,
            frames=total_frames,
        ),
        sound_pack=SoundPackMetadata(
            id=sound_pack.id,
            version=sound_pack.version,
            pack_hash=sound_pack.pack_hash,
            samples=sample_metadata,
        ),
    )


def render_pcm_schedule(
    events: tuple[ScheduledEvent, ...],
    tempo_bpm: Decimal,
    sound_pack: VerifiedSoundPack,
) -> RenderedPerformance:
    try:
        return _render_pcm_schedule(events, tempo_bpm, sound_pack)
    except (MemoryError, OverflowError) as error:
        raise RenderFailure(
            "renderer_resource_unavailable",
            "Renderer resources are unavailable for this Performance.",
            "Renderer allocation or encoding exceeded available resources.",
        ) from error

from __future__ import annotations

import io
import sys
import wave
from array import array
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from play_cli.audio import PcmWavValidationError, validate_pcm16_mono_wav
from play_cli.score import ScheduledEvent
from play_cli.sound_pack import SoundPackFailure, VerifiedSoundPack, read_verified_sample
from play_cli.voice_audio import (
    MAX_OUTPUT_BYTES,
    MAX_VOICES,
    encode_voice,
    encode_voice_samples,
    voice_wav,
)

RENDERER_ID = "pcm-sampler"
RENDERER_VERSION = "3.0"
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
    voices: tuple[tuple[str, bytes], ...]
    gain: float
    peak: int


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


def _pcm_frames(data: bytes, expected_sample_rate_hz: int) -> tuple[bytes, int]:
    try:
        validated = validate_pcm16_mono_wav(
            data,
            expected_sample_rate_hz=expected_sample_rate_hz,
        )
        return validated.frame_data, validated.frames
    except PcmWavValidationError as error:
        raise RenderFailure(
            "renderer_sample_incompatible",
            "Sound Pack sample is incompatible with the renderer.",
        ) from error


def _release_fade(frame_data: bytes, source_frames: int, frame_count: int) -> bytes:
    rendered_frames = min(source_frames, frame_count)
    byte_count = rendered_frames * 2
    if source_frames <= frame_count or byte_count == 0:
        return frame_data[:byte_count]
    fade_frames = min(RELEASE_FADE_FRAMES, rendered_frames)
    fade_start = byte_count - fade_frames * 2
    rendered = array("h")
    rendered.frombytes(frame_data[fade_start:byte_count])
    if sys.byteorder != "little":
        rendered.byteswap()
    denominator = max(1, fade_frames - 1)
    for offset in range(fade_frames):
        remaining = fade_frames - 1 - offset
        magnitude = (abs(rendered[offset]) * remaining + denominator // 2) // denominator
        rendered[offset] = magnitude if rendered[offset] >= 0 else -magnitude
    if sys.byteorder != "little":
        rendered.byteswap()
    return frame_data[:fade_start] + rendered.tobytes()


def _read_back_wav(wav_data: bytes, expected_rate: int, expected_frames: int) -> None:
    try:
        validate_pcm16_mono_wav(
            wav_data,
            expected_sample_rate_hz=expected_rate,
            expected_frames=expected_frames,
        )
    except PcmWavValidationError as error:
        raise RenderFailure("render_failed", "Performance could not be rendered.") from error


def _render_disjoint(
    events: list[tuple[ScheduledEvent, int, int]],
    samples: dict[int, tuple[bytes, int]],
    total_frames: int,
    sample_rate: int,
) -> tuple[bytes, bytes, int]:
    pcm = bytearray(total_frames * 2)
    raw = bytearray(total_frames * 8)
    cache: dict[tuple[int, int], tuple[bytes, bytes, int]] = {}
    peak = 0
    for event, start, end in events:
        if event.pitch is None:
            continue
        key = (event.pitch.midi, end - start)
        cached = cache.get(key)
        if cached is None:
            source, frames = samples[event.pitch.midi]
            note = _release_fade(source, frames, end - start)
            values = array("h")
            values.frombytes(note)
            if sys.byteorder != "little":
                values.byteswap()
            note_peak = max((abs(value) for value in values), default=0)
            cached = (note, encode_voice(note, sample_rate)[56:], note_peak)
            if len(cache) < 64 and len(note) <= sample_rate * 2:
                cache[key] = cached
        note, voice, note_peak = cached
        pcm[start * 2 : start * 2 + len(note)] = note
        raw[start * 8 : start * 8 + len(voice)] = voice
        peak = max(peak, note_peak)
    return bytes(pcm), voice_wav(bytes(raw), sample_rate), peak


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
    voices = sorted({event.voice for event in events})
    if (
        len(voices) > MAX_VOICES
        or total_frames * (2 + 8 * len(voices)) + 100 * len(voices) > MAX_OUTPUT_BYTES
    ):
        raise RenderFailure(
            "renderer_resource_limit", "Performance output exceeds resource limits."
        )
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
    sample_frames = {
        midi: _pcm_frames(payload.data, sample_rate) for midi, payload in payloads.items()
    }
    notes = [(start, end) for event, start, end in event_frames if event.pitch is not None]
    disjoint = len(voices) == 1 and all(a[1] <= b[0] for a, b in zip(notes, notes[1:]))
    if disjoint:
        pcm_data, raw, peak = _render_disjoint(
            event_frames, sample_frames, total_frames, sample_rate
        )
        rendered_voices: tuple[tuple[str, bytes], ...] = ((voices[0], raw),)
        gain = 1.0
    else:
        mixed = array("i", [0]) * total_frames
        voice_samples = {voice: array("i", [0]) * total_frames for voice in voices}
        for event, start, end in event_frames:
            if event.pitch is None:
                continue
            frame_data, source_frames = sample_frames[event.pitch.midi]
            note_data = _release_fade(frame_data, source_frames, end - start)
            values = array("h")
            values.frombytes(note_data)
            if sys.byteorder != "little":
                values.byteswap()
            for offset, value in enumerate(values, start):
                mixed[offset] += value
                voice_samples[event.voice][offset] += value
        high, low = max(mixed, default=0), min(mixed, default=0)
        peak = max(high, -low)
        gain = min(1.0, 32767 / high if high > 0 else 1.0, -32768 / low if low < 0 else 1.0)
        rendered_frames = array("h", (round(value * gain) for value in mixed))
        if sys.byteorder != "little":
            rendered_frames.byteswap()
        pcm_data = rendered_frames.tobytes()
        rendered_voices = tuple(
            (voice, encode_voice_samples(voice_samples[voice], sample_rate)) for voice in voices
        )

    wav_buffer = io.BytesIO()
    try:
        with wave.open(wav_buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(sample_rate)
            output.writeframes(pcm_data)
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
        voices=rendered_voices,
        gain=gain,
        peak=peak,
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

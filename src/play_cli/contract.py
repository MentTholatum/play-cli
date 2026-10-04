from __future__ import annotations

from fractions import Fraction

from play_cli.score import (
    InterpretationWarning,
    KeyContext,
    MeterContext,
    ScheduledEvent,
    ScoreContext,
    ScoreDefaultWarning,
)


def _quarter_value(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


def _exact_quarter_value(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def score_context_json(context: ScoreContext) -> dict[str, object]:
    return {
        "key": {
            "fifths": context.key.fifths,
            "mode": context.key.mode,
            "source": context.key.source,
        },
        "meter": {
            "beats": context.meter.beats,
            "beat_type": context.meter.beat_type,
            "source": context.meter.source,
        },
        "tempo": {"bpm": float(context.tempo.bpm), "source": context.tempo.source},
        "expression": {
            "mode": context.expression.mode,
            "velocity": context.expression.velocity,
            "source": context.expression.source,
        },
    }


def event_json(event: ScheduledEvent) -> dict[str, object]:
    pitch: dict[str, object] | None = None
    if event.pitch is not None:
        pitch = {
            "step": event.pitch.step,
            "alter": event.pitch.alter,
            "octave": event.pitch.octave,
            "midi": event.pitch.midi,
        }
    return {
        "order": event.order,
        "kind": event.kind,
        "onset_quarters": _quarter_value(event.onset),
        "onset_quarters_exact": _exact_quarter_value(event.onset),
        "duration_quarters": _quarter_value(event.duration),
        "duration_quarters_exact": _exact_quarter_value(event.duration),
        "pitch": pitch,
        "velocity": event.velocity,
        "voice": event.voice,
        "staff": event.staff,
        "staves": list(event.source_staves or (event.staff,)),
        "event_id": f"event-{event.order + 1:06d}",
        "sources": list(event.sources),
        "pages": list(event.source_pages),
    }


def warning_json(warning: ScoreDefaultWarning | InterpretationWarning) -> dict[str, object]:
    if isinstance(warning, InterpretationWarning):
        return {
            "id": "score_interpretation_uncertain",
            "kind": "uncertainty",
            "location": warning.location,
            "message": warning.message,
            "chosen_interpretation": warning.chosen_interpretation,
        }
    if isinstance(warning.applied_value, KeyContext):
        applied_value: dict[str, object] = {
            "fifths": warning.applied_value.fifths,
            "mode": warning.applied_value.mode,
        }
    elif isinstance(warning.applied_value, MeterContext):
        applied_value = {
            "beats": warning.applied_value.beats,
            "beat_type": warning.applied_value.beat_type,
        }
    else:
        applied_value = {"bpm": float(warning.applied_value.bpm)}
    return {
        "id": warning.id,
        "kind": "score_default",
        "field": warning.field,
        "message": warning.message,
        "applied_value": applied_value,
    }

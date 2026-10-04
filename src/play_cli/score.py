from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Literal, TypeVar

from play_cli.voice_audio import MAX_VOICES

MAX_SCORE_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 10000
MAX_POLYPHONY = 64

NEUTRAL_VELOCITY = 64
SEMITONES_FROM_C = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
NOTE_TYPE_QUARTERS = {
    "whole": Fraction(4),
    "half": Fraction(2),
    "quarter": Fraction(1),
    "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4),
    "32nd": Fraction(1, 8),
    "64th": Fraction(1, 16),
}


@dataclass(frozen=True)
class _DirectionChildPolicy:
    name: str
    minimum: int
    maximum: int | None
    supported: bool


DIRECTION_CHILD_POLICY = (
    _DirectionChildPolicy("direction-type", 1, None, True),
    _DirectionChildPolicy("offset", 0, 1, False),
    _DirectionChildPolicy("footnote", 0, 1, True),
    _DirectionChildPolicy("level", 0, 1, True),
    _DirectionChildPolicy("voice", 0, 1, True),
    _DirectionChildPolicy("staff", 0, 1, True),
    _DirectionChildPolicy("sound", 0, 1, True),
    _DirectionChildPolicy("listening", 0, 1, False),
)
DIRECTION_ALLOWED_CHILDREN = frozenset(policy.name for policy in DIRECTION_CHILD_POLICY)
DIRECTION_CHILD_CARDINALITY = {
    policy.name: (policy.minimum, policy.maximum) for policy in DIRECTION_CHILD_POLICY
}
DIRECTION_CHILD_ORDER = {policy.name: index for index, policy in enumerate(DIRECTION_CHILD_POLICY)}
DIRECTION_UNSUPPORTED_CHILDREN = frozenset(
    ("direction", policy.name) for policy in DIRECTION_CHILD_POLICY if not policy.supported
)

ALLOWED_CHILDREN = {
    "score-partwise": frozenset(
        {
            "work",
            "movement-number",
            "movement-title",
            "identification",
            "defaults",
            "credit",
            "part-list",
            "part",
        }
    ),
    "part-list": frozenset({"part-group", "score-part"}),
    "score-part": frozenset(
        {
            "identification",
            "part-link",
            "part-name",
            "part-name-display",
            "part-abbreviation",
            "part-abbreviation-display",
            "group",
            "score-instrument",
            "player",
            "midi-device",
            "midi-instrument",
        }
    ),
    "part": frozenset({"measure"}),
    "measure": frozenset(
        {"print", "attributes", "direction", "note", "barline", "backup", "forward"}
    ),
    "backup": frozenset({"duration"}),
    "forward": frozenset({"duration", "voice", "staff"}),
    "attributes": frozenset(
        {"divisions", "key", "time", "staves", "part-symbol", "clef", "staff-details"}
    ),
    "key": frozenset({"cancel", "fifths", "mode"}),
    "time": frozenset({"beats", "beat-type"}),
    "direction": DIRECTION_ALLOWED_CHILDREN,
    "direction-type": frozenset({"metronome"}),
    "metronome": frozenset({"beat-unit", "beat-unit-dot", "per-minute"}),
    "sound": frozenset(),
    "note": frozenset(
        {
            "chord",
            "pitch",
            "rest",
            "duration",
            "tie",
            "voice",
            "type",
            "time-modification",
            "dot",
            "accidental",
            "stem",
            "notehead",
            "notehead-text",
            "staff",
            "beam",
            "notations",
            "lyric",
        }
    ),
    "pitch": frozenset({"step", "alter", "octave"}),
    "chord": frozenset(),
    "rest": frozenset({"display-step", "display-octave"}),
    "notations": frozenset({"footnote", "level", "tied"}),
    "tie": frozenset(),
    "tied": frozenset(),
    "barline": frozenset({"bar-style", "footnote", "level"}),
}
ALLOWED_ATTRIBUTES = {
    "chord": frozenset(),
    "backup": frozenset(),
    "forward": frozenset(),
    "score-partwise": frozenset({"version"}),
    "part-list": frozenset(),
    "score-part": frozenset({"id"}),
    "part": frozenset({"id"}),
    "measure": frozenset({"id", "implicit", "non-controlling", "number", "text", "width"}),
    "attributes": frozenset(),
    "divisions": frozenset(),
    "key": frozenset({"number", "print-object"}),
    "fifths": frozenset(),
    "mode": frozenset(),
    "time": frozenset({"number", "print-object", "separator", "symbol"}),
    "beats": frozenset(),
    "beat-type": frozenset(),
    "staves": frozenset(),
    "direction": frozenset(
        {
            "color",
            "default-x",
            "default-y",
            "directive",
            "font-family",
            "font-size",
            "font-style",
            "font-weight",
            "id",
            "placement",
            "relative-x",
            "relative-y",
            "system",
        }
    ),
    "direction-type": frozenset(),
    "metronome": frozenset(
        {
            "color",
            "default-x",
            "default-y",
            "font-family",
            "font-size",
            "font-style",
            "font-weight",
            "halign",
            "justify",
            "parentheses",
            "relative-x",
            "relative-y",
            "valign",
        }
    ),
    "beat-unit": frozenset(),
    "beat-unit-dot": frozenset(),
    "per-minute": frozenset({"font-family", "font-size", "font-style", "font-weight"}),
    "note": frozenset(
        {
            "color",
            "default-x",
            "default-y",
            "font-family",
            "font-size",
            "font-style",
            "font-weight",
            "id",
            "print-dot",
            "print-leger",
            "print-lyric",
            "print-object",
            "print-spacing",
            "relative-x",
            "relative-y",
        }
    ),
    "pitch": frozenset(),
    "step": frozenset(),
    "alter": frozenset(),
    "octave": frozenset(),
    "rest": frozenset({"measure"}),
    "duration": frozenset(),
    "tie": frozenset({"type"}),
    "voice": frozenset(),
    "type": frozenset({"size"}),
    "time-modification": frozenset({"actual-notes", "normal-notes", "normal-type", "normal-dot"}),
    "actual-notes": frozenset(),
    "normal-notes": frozenset(),
    "normal-type": frozenset(),
    "normal-dot": frozenset(),
    "accidental": frozenset(
        {
            "bracket",
            "cautionary",
            "color",
            "default-x",
            "default-y",
            "editorial",
            "font-family",
            "font-size",
            "font-style",
            "font-weight",
            "parentheses",
            "relative-x",
            "relative-y",
            "size",
        }
    ),
    "staff": frozenset(),
    "notations": frozenset({"print-object"}),
    "dot": frozenset({"color", "default-x", "default-y", "placement", "relative-x", "relative-y"}),
    "stem": frozenset({"color", "default-x", "default-y", "relative-x", "relative-y"}),
    "notehead": frozenset(
        {
            "color",
            "default-x",
            "default-y",
            "filled",
            "font-family",
            "font-size",
            "font-style",
            "font-weight",
            "parentheses",
            "relative-x",
            "relative-y",
        }
    ),
    "beam": frozenset({"color", "fan", "id", "number", "repeater"}),
    "tied": frozenset(
        {
            "bezier-offset",
            "bezier-offset2",
            "bezier-x",
            "bezier-x2",
            "bezier-y",
            "bezier-y2",
            "color",
            "dash-length",
            "default-x",
            "default-y",
            "line-type",
            "number",
            "orientation",
            "placement",
            "relative-x",
            "relative-y",
            "space-length",
            "id",
            "type",
        }
    ),
    "barline": frozenset({"coda", "divisions", "location", "segno"}),
    "bar-style": frozenset({"color"}),
    "sound": frozenset({"tempo"}),
}
CHILD_CARDINALITY: dict[str, dict[str, tuple[int, int | None]]] = {
    "backup": {"duration": (1, 1)},
    "forward": {"duration": (1, 1), "voice": (0, 1), "staff": (0, 1)},
    "score-partwise": {
        "work": (0, 1),
        "movement-number": (0, 1),
        "movement-title": (0, 1),
        "identification": (0, 1),
        "defaults": (0, 1),
        "part-list": (1, 1),
        "part": (1, None),
    },
    "part-list": {"score-part": (1, 1)},
    "score-part": {"part-name": (1, 1)},
    "part": {"measure": (1, None)},
    "measure": {"print": (0, 1)},
    "direction": DIRECTION_CHILD_CARDINALITY,
    "direction-type": {"metronome": (1, 1)},
    "attributes": {
        "divisions": (0, 1),
        "key": (0, 1),
        "time": (0, 1),
        "staves": (0, 1),
    },
    "key": {"cancel": (0, 1), "fifths": (1, 1), "mode": (0, 1)},
    "time": {"beats": (1, 1), "beat-type": (1, 1)},
    "note": {
        "chord": (0, 1),
        "pitch": (0, 1),
        "rest": (0, 1),
        "duration": (1, 1),
        "voice": (0, 1),
        "type": (0, 1),
        "accidental": (0, 1),
        "staff": (0, 1),
        "notations": (0, 1),
        "time-modification": (0, 1),
    },
    "time-modification": {
        "actual-notes": (1, 1),
        "normal-notes": (1, 1),
        "normal-type": (0, 1),
        "normal-dot": (0, None),
    },
    "pitch": {"step": (1, 1), "alter": (0, 1), "octave": (1, 1)},
    "metronome": {
        "beat-unit": (1, 1),
        "beat-unit-dot": (0, 1),
        "per-minute": (1, 1),
    },
}
UNSUPPORTED_CHILDREN = DIRECTION_UNSUPPORTED_CHILDREN
VALUE_ELEMENTS = frozenset(
    {
        "accidental",
        "alter",
        "beat-type",
        "beat-unit",
        "beats",
        "divisions",
        "duration",
        "fifths",
        "mode",
        "octave",
        "per-minute",
        "staff",
        "staves",
        "step",
        "type",
        "actual-notes",
        "normal-notes",
        "normal-type",
        "voice",
    }
)
HARMLESS_SUBTREES = frozenset(
    {
        "work",
        "identification",
        "defaults",
        "credit",
        "part-group",
        "part-link",
        "part-name",
        "part-name-display",
        "part-abbreviation",
        "part-abbreviation-display",
        "group",
        "score-instrument",
        "player",
        "midi-device",
        "midi-instrument",
        "print",
        "part-symbol",
        "clef",
        "staff-details",
        "notehead-text",
        "lyric",
    }
)

ContextSource = Literal["score", "default"]
EventKind = Literal["note", "rest"]
ScoreDefaultId = Literal["score_default_key", "score_default_meter", "score_default_tempo"]
ScoreDefaultField = Literal["key", "meter", "tempo"]
ScoreErrorId = Literal["musicxml_invalid", "notation_unsupported"]
ContextValue = TypeVar("ContextValue")


@dataclass(frozen=True)
class Pitch:
    step: str
    alter: int
    octave: int
    midi: int


@dataclass(frozen=True)
class ScheduledEvent:
    order: int
    kind: EventKind
    onset: Fraction
    duration: Fraction
    pitch: Pitch | None
    velocity: int
    voice: str = "1"
    staff: int = 1
    sources: tuple[str, ...] = ()
    source_staves: tuple[int, ...] = ()
    source_pages: tuple[int, ...] = (1,)


@dataclass(frozen=True)
class KeyContext:
    fifths: int
    mode: str | None
    source: ContextSource


@dataclass(frozen=True)
class MeterContext:
    beats: int
    beat_type: int
    source: ContextSource


@dataclass(frozen=True)
class TempoContext:
    bpm: Decimal
    source: ContextSource


@dataclass(frozen=True)
class ExpressionContext:
    mode: Literal["neutral"]
    velocity: int
    source: ContextSource


@dataclass(frozen=True)
class ScoreContext:
    key: KeyContext
    meter: MeterContext
    tempo: TempoContext
    expression: ExpressionContext


@dataclass(frozen=True)
class ScoreDefaultWarning:
    id: ScoreDefaultId
    field: ScoreDefaultField
    message: str
    applied_value: KeyContext | MeterContext | TempoContext


@dataclass(frozen=True)
class InterpretationWarning:
    location: str
    message: str
    chosen_interpretation: str


@dataclass(frozen=True)
class ValidatedScore:
    context: ScoreContext
    events: tuple[ScheduledEvent, ...]
    warnings: tuple[ScoreDefaultWarning | InterpretationWarning, ...]


@dataclass
class _PendingTie:
    onset: Fraction
    duration: Fraction
    pitch: Pitch


class ScoreFailure(Exception):
    def __init__(self, error_id: ScoreErrorId, message: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.error_id = error_id
        self.message = message
        self.diagnostic = diagnostic


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child) == name]


def _unsupported(diagnostic: str) -> ScoreFailure:
    return ScoreFailure(
        "notation_unsupported",
        "Playable Score contains unsupported notation.",
        f"Unsupported MusicXML notation: {diagnostic}",
    )


def _invalid(diagnostic: str) -> ScoreFailure:
    return ScoreFailure(
        "musicxml_invalid",
        "Playable Score MusicXML is invalid.",
        diagnostic,
    )


def _validate_child_cardinality(element: ET.Element, name: str) -> None:
    for child_name, (minimum, maximum) in CHILD_CARDINALITY.get(name, {}).items():
        count = len(_children(element, child_name))
        if count < minimum:
            raise _invalid(f"Missing MusicXML element: {child_name} under {name}")
        if maximum is not None and count > maximum:
            raise _invalid(f"Duplicated MusicXML element: {child_name} under {name}")


def _validate_direction_child_order(element: ET.Element) -> None:
    positions = [DIRECTION_CHILD_ORDER[_local_name(child)] for child in element]
    if positions != sorted(positions):
        raise _invalid("Invalid MusicXML child order under direction.")


def _validate_supported_tree(element: ET.Element) -> None:
    name = _local_name(element)
    allowed_attributes = ALLOWED_ATTRIBUTES.get(name)
    if allowed_attributes is not None:
        for attribute in element.attrib:
            attribute_name = attribute.rsplit("}", 1)[-1]
            if attribute_name not in allowed_attributes:
                raise _unsupported(f"{attribute_name} attribute on {name}")
    if name in VALUE_ELEMENTS and (element.text is None or not element.text.strip()):
        raise _invalid(f"MusicXML element has no value: {name}")
    if name in HARMLESS_SUBTREES:
        return
    if (
        name in {"staff", "staves"}
        and element.text is not None
        and element.text.strip() not in {"1", "2"}
    ):
        raise _unsupported("staff count or reference outside one piano's two staves")

    allowed = ALLOWED_CHILDREN.get(name)
    if allowed is None:
        return
    for child in element:
        child_name = _local_name(child)
        if child_name not in allowed:
            raise _unsupported(f"{child_name} under {name}")
    _validate_child_cardinality(element, name)
    if name == "direction":
        _validate_direction_child_order(element)
    for child in element:
        child_name = _local_name(child)
        if (name, child_name) in UNSUPPORTED_CHILDREN:
            raise _unsupported(f"{child_name} under {name}")
        _validate_supported_tree(child)


def _child(element: ET.Element, name: str) -> ET.Element | None:
    matches = _children(element, name)
    if len(matches) > 1:
        raise _invalid(f"Duplicated MusicXML element: {name} under {_local_name(element)}")
    return matches[0] if matches else None


def _required_child(element: ET.Element, name: str) -> ET.Element:
    child = _child(element, name)
    if child is None:
        raise _invalid(f"Missing MusicXML element: {name}")
    return child


def _required_text(element: ET.Element, name: str) -> str:
    child = _required_child(element, name)
    if child.text is None or not child.text.strip():
        raise _invalid(f"MusicXML element has no value: {name}")
    return child.text.strip()


def _required_attribute(element: ET.Element, name: str) -> str:
    value = element.get(name)
    if value is None or not value.strip():
        raise _invalid(
            f"MusicXML element is missing a non-empty {name} attribute: {_local_name(element)}"
        )
    return value.strip()


def _validate_part_identity(root: ET.Element, part: ET.Element) -> None:
    part_list = _required_child(root, "part-list")
    score_part = _required_child(part_list, "score-part")
    score_part_id = _required_attribute(score_part, "id")
    part_id = _required_attribute(part, "id")
    if score_part_id != part_id:
        raise _invalid(
            f"MusicXML score-part id {score_part_id!r} does not match part id {part_id!r}."
        )


def _integer(text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError as error:
        raise _invalid(f"MusicXML value is not an integer: {field}") from error


def _positive_integer(text: str, field: str) -> int:
    value = _integer(text, field)
    if value <= 0:
        raise _invalid(f"MusicXML value must be positive: {field}")
    return value


def _parse_pitch(note: ET.Element) -> Pitch | None:
    pitch_element = _child(note, "pitch")
    rest_element = _child(note, "rest")
    if (pitch_element is None) == (rest_element is None):
        raise _invalid("Each note must contain exactly one pitch or rest.")
    if pitch_element is None:
        return None

    step = _required_text(pitch_element, "step")
    if step not in SEMITONES_FROM_C:
        raise _invalid(f"MusicXML pitch step is invalid: {step}")
    alter_element = _child(pitch_element, "alter")
    alter = (
        _integer(alter_element.text.strip(), "alter")
        if alter_element is not None and alter_element.text
        else 0
    )
    octave = _integer(_required_text(pitch_element, "octave"), "octave")
    midi = (octave + 1) * 12 + SEMITONES_FROM_C[step] + alter
    if not 0 <= midi <= 127:
        raise _invalid("MusicXML pitch is outside the supported MIDI range.")
    return Pitch(step=step, alter=alter, octave=octave, midi=midi)


def _validate_notated_duration(note: ET.Element, duration: Fraction) -> None:
    type_element = _child(note, "type")
    dots = len(_children(note, "dot"))
    if type_element is None and dots == 0:
        return
    if type_element is None or type_element.text is None:
        raise _invalid("A dotted note must declare its note type.")
    base = NOTE_TYPE_QUARTERS.get(type_element.text.strip())
    if base is None:
        raise _unsupported(f"note type {type_element.text.strip()}")
    factor = sum((Fraction(1, 2**index) for index in range(dots + 1)), Fraction())
    time_modification = _child(note, "time-modification")
    tuplet_factor = Fraction(1)
    if time_modification is not None:
        actual = _positive_integer(
            _required_text(time_modification, "actual-notes"), "actual-notes"
        )
        normal = _positive_integer(
            _required_text(time_modification, "normal-notes"), "normal-notes"
        )
        tuplet_factor = Fraction(normal, actual)
    if base * factor * tuplet_factor != duration:
        raise _invalid("MusicXML type and dots do not match duration/divisions.")


def _parse_ties(note: ET.Element) -> frozenset[str]:
    tie_types = [tie.get("type") for tie in _children(note, "tie")]
    if any(tie_type not in {"start", "stop"} for tie_type in tie_types):
        raise _invalid("MusicXML tie type must be start or stop.")
    if len(tie_types) != len(set(tie_types)):
        raise _invalid("MusicXML tie type is duplicated.")

    notations = _child(note, "notations")
    if notations is not None:
        tied_types = [tied.get("type") for tied in _children(notations, "tied")]
        if tied_types and set(tied_types) != set(tie_types):
            raise _invalid("MusicXML tie and tied notation do not match.")
    return frozenset(tie_type for tie_type in tie_types if tie_type is not None)


def _parse_voice(element: ET.Element) -> str | None:
    voice = _child(element, "voice")
    if voice is None:
        return None
    if voice.text is None or not voice.text.strip():
        raise _invalid("MusicXML voice must contain a non-empty value.")
    return voice.text.strip()


def _merge_voice(current: str | None, parsed: str | None) -> str | None:
    if current is not None and parsed is not None and parsed != current:
        raise _unsupported("multiple voice values")
    return parsed or current


def _parse_key(attributes: ET.Element) -> KeyContext | None:
    key = _child(attributes, "key")
    if key is None:
        return None
    fifths = _integer(_required_text(key, "fifths"), "fifths")
    mode_element = _child(key, "mode")
    mode = mode_element.text.strip() if mode_element is not None and mode_element.text else None
    return KeyContext(fifths=fifths, mode=mode, source="score")


def _parse_meter(attributes: ET.Element) -> MeterContext | None:
    time = _child(attributes, "time")
    if time is None:
        return None
    beats = _positive_integer(_required_text(time, "beats"), "beats")
    beat_type = _positive_integer(_required_text(time, "beat-type"), "beat-type")
    return MeterContext(beats=beats, beat_type=beat_type, source="score")


def _decimal(text: str, field: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise _invalid(f"MusicXML value is not numeric: {field}") from error
    if not value.is_finite() or value <= 0:
        raise _invalid(f"MusicXML value must be positive: {field}")
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value <= 0:
        raise _invalid(f"MusicXML value is outside the supported numeric range: {field}")
    return value


def _parse_tempo(direction: ET.Element) -> TempoContext | None:
    values: list[Decimal] = []
    for element in direction.iter():
        if _local_name(element) == "sound" and element.get("tempo") is not None:
            values.append(_decimal(element.get("tempo", ""), "tempo"))
        if _local_name(element) == "metronome":
            beat_unit = _required_text(element, "beat-unit")
            if beat_unit != "quarter" or _children(element, "beat-unit-dot"):
                raise _unsupported("metronome tempo other than an undotted quarter note")
            values.append(_decimal(_required_text(element, "per-minute"), "per-minute"))
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise _invalid("MusicXML tempo declarations do not agree.")
    return TempoContext(bpm=values[0], source="score")


def _retain_initial_context(
    current: ContextValue | None,
    parsed: ContextValue | None,
    field: str,
    *,
    musical_started: bool,
) -> ContextValue | None:
    if parsed is None:
        return current
    if current is None and musical_started:
        raise _unsupported(f"{field} declaration after music started")
    if current is not None and parsed != current:
        raise _unsupported(f"{field} change after the initial context")
    return parsed


def _append_event(
    events: list[ScheduledEvent],
    *,
    kind: EventKind,
    onset: Fraction,
    duration: Fraction,
    pitch: Pitch | None,
) -> None:
    events.append(
        ScheduledEvent(
            order=len(events),
            kind=kind,
            onset=onset,
            duration=duration,
            pitch=pitch,
            velocity=NEUTRAL_VELOCITY if pitch is not None else 0,
        )
    )


def parse_score(score_path: Path) -> ValidatedScore:
    return parse_score_bytes(score_path.read_bytes())


def parse_score_bytes(score_bytes: bytes) -> ValidatedScore:
    if len(score_bytes) > MAX_SCORE_BYTES:
        raise _unsupported("score byte limit exceeded")
    try:
        root = ET.fromstring(score_bytes)
    except (ET.ParseError, LookupError, ValueError) as error:
        raise _invalid(f"MusicXML syntax error: {error}") from error
    return _validated_score(root)


def _validated_score(root: ET.Element) -> ValidatedScore:

    if _local_name(root) != "score-partwise":
        raise _unsupported("score type other than score-partwise")
    parts = _children(root, "part")
    if len(parts) > 1:
        raise _unsupported("part count other than one")
    if sum(_local_name(element) in {"note", "forward"} for element in root.iter()) > MAX_EVENTS:
        raise _unsupported("event limit exceeded")
    _validate_supported_tree(root)
    _validate_part_identity(root, parts[0])

    divisions: int | None = None
    key: KeyContext | None = None
    meter: MeterContext | None = None
    tempo: TempoContext | None = None
    cursor = Fraction()
    events: list[ScheduledEvent] = []
    voice_names: set[str] = set()
    direction_voices: set[str] = set()
    staves = 1
    measure_start = Fraction()
    measure_span = Fraction()
    musical_started = False
    pending_ties: dict[tuple[str, Pitch], int] = {}
    page = 1
    declared_voices = {
        _parse_voice(element) or "1"
        for element in root.iter()
        if _local_name(element) in {"note", "forward"}
    }
    polyphonic = len(declared_voices) > 1 or any(
        _local_name(element) == "chord"
        or (_local_name(element) == "staves" and (element.text or "").strip() == "2")
        for element in root.iter()
    )

    for measure_index, measure in enumerate(_children(parts[0], "measure"), 1):
        measure_has_music = False
        cursor = measure_start
        measure_span = Fraction()
        chord_anchor: ScheduledEvent | None = None
        voice_ends: dict[str, Fraction] = {}
        for element in measure:
            name = _local_name(element)
            if name == "print" and element.get("new-page") == "yes" and measure_index > 1:
                page += 1
            elif name == "attributes":
                divisions_element = _child(element, "divisions")
                if divisions_element is not None and divisions_element.text:
                    if measure_has_music:
                        raise _unsupported("divisions change within a measure")
                    divisions = _positive_integer(divisions_element.text.strip(), "divisions")
                parsed_key = _parse_key(element)
                key = _retain_initial_context(
                    key, parsed_key, "key", musical_started=musical_started
                )
                parsed_meter = _parse_meter(element)
                meter = _retain_initial_context(
                    meter, parsed_meter, "meter", musical_started=musical_started
                )
                declared_staves = _child(element, "staves")
                if declared_staves is not None:
                    parsed_staves = _positive_integer(declared_staves.text or "", "staves")
                    if musical_started and parsed_staves != staves:
                        raise _unsupported("staves change after music started")
                    staves = parsed_staves
            elif name == "direction":
                direction_voice = _parse_voice(element)
                if direction_voice is not None:
                    direction_voices.add(direction_voice)
                parsed_tempo = _parse_tempo(element)
                tempo = _retain_initial_context(
                    tempo, parsed_tempo, "tempo", musical_started=musical_started
                )
            elif name in {"backup", "forward"}:
                measure_has_music = True
                musical_started = True
                chord_anchor = None
                if divisions is None:
                    raise _invalid("MusicXML divisions must precede cursor movement.")
                movement = Fraction(
                    _positive_integer(_required_text(element, "duration"), "duration"), divisions
                )
                if name == "backup":
                    cursor -= movement
                    if cursor < measure_start:
                        raise _invalid("MusicXML backup moves before the measure start.")
                else:
                    voice = _parse_voice(element)
                    if voice is None:
                        raise _invalid("Forward requires an explicit voice to retain silence.")
                    staff = (
                        _positive_integer(_required_text(element, "staff"), "staff")
                        if _child(element, "staff") is not None
                        else 1
                    )
                    if staff > staves:
                        raise _invalid("Staff reference exceeds declared staves.")
                    if cursor < voice_ends.get(voice, measure_start):
                        raise _invalid("Forward overlaps existing music in the same voice.")
                    voice_ends[voice] = cursor + movement
                    events.append(
                        ScheduledEvent(
                            len(events),
                            "rest",
                            cursor,
                            movement,
                            None,
                            0,
                            voice,
                            staff,
                            (f"measure:{measure_index}",),
                            (staff,),
                            (page,),
                        )
                    )
                    voice_names.add(voice)
                    cursor += movement
                    measure_span = max(measure_span, cursor - measure_start)
            elif name == "note":
                measure_has_music = True
                musical_started = True
                voice = _parse_voice(element) or "1"
                voice_names.add(voice)
                staff = (
                    _positive_integer(_required_text(element, "staff"), "staff")
                    if _child(element, "staff") is not None
                    else 1
                )
                if staff > staves:
                    raise _invalid("Staff reference exceeds declared staves.")
                if divisions is None:
                    raise _invalid("MusicXML divisions must be declared before notes.")
                duration = Fraction(
                    _positive_integer(_required_text(element, "duration"), "duration"),
                    divisions,
                )
                _validate_notated_duration(element, duration)
                pitch = _parse_pitch(element)
                ties = _parse_ties(element)
                if _child(element, "chord") is not None:
                    if (
                        chord_anchor is None
                        or pitch is None
                        or duration > chord_anchor.duration
                        or voice != chord_anchor.voice
                    ):
                        raise _invalid("Invalid chord anchor or duration.")
                    onset = chord_anchor.onset
                else:
                    onset = cursor
                    if onset < voice_ends.get(voice, measure_start):
                        raise _invalid("Overlapping notes in a voice require chord notation.")
                    voice_ends[voice] = onset + duration
                    cursor += duration
                event = ScheduledEvent(
                    len(events),
                    "note" if pitch else "rest",
                    onset,
                    duration,
                    pitch,
                    NEUTRAL_VELOCITY if pitch else 0,
                    voice,
                    staff,
                    (f"measure:{measure_index}",),
                    (staff,),
                    (page,),
                )
                if pitch is None and ties:
                    raise _invalid("A tie cannot apply to a rest.")
                tie_key = (voice, pitch) if pitch is not None else None
                if "stop" in ties and tie_key is not None:
                    tie_index = pending_ties.get(tie_key)
                    if tie_index is None:
                        raise _invalid("MusicXML tie stop has no matching start.")
                    previous = events[tie_index]
                    if previous.onset + previous.duration != onset:
                        raise _invalid("MusicXML tie chain is not contiguous.")
                    events[tie_index] = replace(
                        previous,
                        duration=previous.duration + duration,
                        sources=previous.sources + event.sources,
                        source_staves=tuple(sorted(set(previous.source_staves + (staff,)))),
                        source_pages=tuple(sorted(set(previous.source_pages + (page,)))),
                    )
                    if "start" not in ties:
                        del pending_ties[tie_key]
                else:
                    if tie_key in pending_ties:
                        raise _invalid("MusicXML tie chain has no matching stop.")
                    if "start" in ties and tie_key is not None:
                        pending_ties[tie_key] = len(events)
                    events.append(event)
                if _child(element, "chord") is None:
                    chord_anchor = event if pitch else None
                measure_span = max(measure_span, onset + duration - measure_start)
        if polyphonic:
            expected = Fraction(meter.beats * 4, meter.beat_type) if meter else Fraction(4)
            if not 0 < measure_span <= expected or (
                measure_span != expected and measure.get("implicit") != "yes"
            ):
                raise _invalid("Polyphonic measure span must match meter or declare a pickup.")
        measure_start += measure_span

    if not direction_voices <= voice_names:
        raise _unsupported("direction references an absent voice")
    if pending_ties:
        raise _invalid("MusicXML tie chain has no matching stop.")
    if len(voice_names) > MAX_VOICES:
        raise _unsupported("voice limit exceeded")
    changes = sorted(
        (time, delta)
        for event in events
        if event.pitch is not None
        for time, delta in ((event.onset, 1), (event.onset + event.duration, -1))
    )
    active = 0
    for _, delta in changes:
        active += delta
        if active > MAX_POLYPHONY:
            raise _unsupported("simultaneous note limit exceeded")
    events = [
        replace(event, order=index)
        for index, event in enumerate(sorted(events, key=lambda e: (e.onset, e.order)))
    ]
    if not events:
        raise _invalid("MusicXML score contains no musical events.")

    warnings: list[ScoreDefaultWarning | InterpretationWarning] = []
    if key is None:
        key = KeyContext(fifths=0, mode="major", source="default")
        warnings.append(
            ScoreDefaultWarning(
                id="score_default_key",
                field="key",
                message="Key signature was absent; C major was applied.",
                applied_value=key,
            )
        )
    if meter is None:
        meter = MeterContext(beats=4, beat_type=4, source="default")
        warnings.append(
            ScoreDefaultWarning(
                id="score_default_meter",
                field="meter",
                message="Meter was absent; 4/4 was applied.",
                applied_value=meter,
            )
        )
    if tempo is None:
        tempo = TempoContext(bpm=Decimal(100), source="default")
        warnings.append(
            ScoreDefaultWarning(
                id="score_default_tempo",
                field="tempo",
                message="Tempo was absent; 100 BPM was applied.",
                applied_value=tempo,
            )
        )

    root_metadata_fields = {
        field
        for identification in _children(root, "identification")
        for miscellaneous in _children(identification, "miscellaneous")
        for field in _children(miscellaneous, "miscellaneous-field")
    }
    for field in root.iter():
        if (
            _local_name(field) != "miscellaneous-field"
            or field.get("name") != "play-cli:uncertainty"
        ):
            continue
        if field not in root_metadata_fields:
            raise _invalid("Interpretation uncertainty must be in root identification metadata.")
        try:
            value = json.loads(field.text or "")
        except (ValueError, RecursionError) as error:
            raise _invalid("Interpretation uncertainty must contain a JSON object.") from error
        keys = {"location", "message", "chosen_interpretation"}
        if (
            len(field) != 0
            or not isinstance(value, dict)
            or set(value) != keys
            or any(not isinstance(value[key], str) or not value[key].strip() for key in keys)
        ):
            raise _invalid("Interpretation uncertainty requires three non-empty text fields.")
        warnings.append(InterpretationWarning(**value))

    return ValidatedScore(
        context=ScoreContext(
            key=key,
            meter=meter,
            tempo=tempo,
            expression=ExpressionContext(
                mode="neutral", velocity=NEUTRAL_VELOCITY, source="default"
            ),
        ),
        events=tuple(events),
        warnings=tuple(warnings),
    )

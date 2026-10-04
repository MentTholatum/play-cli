from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CLI = Path(sys.executable).with_name("playcli")


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(CLI), *args], text=True, capture_output=True, check=False)


def score_document(measures: str) -> str:
    return (
        '<score-partwise version="4.0"><part-list><score-part id="P1">'
        '<part-name>Piano</part-name></score-part></part-list><part id="P1">'
        + measures
        + "</part></score-partwise>"
    )


def test_agent_retains_cross_staff_tie_as_one_attack(tmp_path: Path) -> None:
    score = tmp_path / "tie.musicxml"
    score.write_text(
        score_document("""
      <measure number="0" implicit="yes"><attributes>
      <divisions>1</divisions><staves>2</staves></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
      <tie type="start"/><voice>a</voice><staff>1</staff></note>
      <backup><duration>1</duration></backup>
      <note><rest/><duration>1</duration><voice>b</voice><staff>2</staff></note></measure>
      <measure number="1">
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration>
      <tie type="stop"/><voice>a</voice><staff>2</staff></note>
      <backup><duration>4</duration></backup>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>b</voice><staff>1</staff></note></measure>""")
    )
    result = run("validate", str(score))
    assert result.returncode == 0, result.stderr
    events = json.loads(result.stdout)["events"]
    assert [
        (e["voice"], e["onset_quarters"], e["duration_quarters"])
        for e in events
        if e["kind"] == "note"
    ] == [("a", 0, 5), ("b", 1, 4)]
    assert events[0]["sources"] == ["measure:1", "measure:2"]


def test_agent_aligns_two_voices_and_retains_silent_tail(tmp_path: Path) -> None:
    score = tmp_path / "voices.musicxml"
    score.write_text(
        score_document("""<measure number="1"><attributes><divisions>1</divisions>
      <staves>2</staves></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration>
        <voice>upper</voice><staff>1</staff></note>
      <note><rest/><duration>2</duration><voice>upper</voice><staff>1</staff></note>
      <backup><duration>4</duration></backup>
      <forward><duration>1</duration><voice>lower</voice><staff>2</staff></forward>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration>
        <voice>lower</voice><staff>2</staff></note>
      <forward><duration>2</duration><voice>lower</voice><staff>2</staff></forward>
      </measure>""")
    )
    result = run("validate", str(score))
    assert result.returncode == 0, result.stderr
    events = json.loads(result.stdout)["events"]
    assert [
        (e["voice"], e["onset_quarters"], e["duration_quarters"])
        for e in events
        if e["kind"] == "note"
    ] == [("upper", 0, 2), ("lower", 1, 1)]
    store = tmp_path / "packs"
    assert (
        run(
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/test-piano-pack"),
            "--store",
            str(store),
        ).returncode
        == 0
    )
    result = run(
        "render",
        str(score),
        "--sound-pack",
        "play-cli-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
        "--output",
        str(tmp_path / "duet"),
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["wav"]["frames"] == 38400  # 4 quarter notes, 100 BPM, 16 kHz.
    voices = manifest["result_bundle"]["voices"]
    assert {v["voice"] for v in voices} == {"upper", "lower"}
    lower = next(v for v in voices if v["voice"] == "lower")
    data = (tmp_path / "duet" / lower["relative_path"]).read_bytes()
    assert data[56 : 56 + 9600 * 8] == bytes(9600 * 8)
    assert data[56 + 19200 * 8 :] == bytes(19200 * 8)


def test_agent_renders_chord_without_clipping_original_voice(tmp_path: Path) -> None:
    source = (ROOT / "tests/fixtures/renderable-c4.musicxml").read_text()
    # Eight simultaneous copies must remain eight attacks, even at identical pitch.
    start, end = source.index("<note "), source.index("</note>") + len("</note>")
    note = "<note>" + source[source.index(">", start) + 1 : end]
    source = source[:end] + note.replace("<note>", "<note><chord/>") * 7 + source[end:]
    source = source.replace('<measure number="1">', '<measure number="1" implicit="yes">')
    score = tmp_path / "chord.musicxml"
    score.write_text(source)
    store = tmp_path / "packs"
    assert (
        run(
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/test-piano-pack"),
            "--store",
            str(store),
        ).returncode
        == 0
    )
    result = run(
        "render",
        str(score),
        "--sound-pack",
        "play-cli-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
        "--output",
        str(tmp_path / "chord"),
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["mix"]["gain"] < 1
    data = (tmp_path / "chord/voice-0001.wav").read_bytes()
    values = [v[0] for v in struct.iter_unpack("<d", data[56:])]
    assert max(abs(v) for v in values) > 1
    assert any(w["id"] == "mix_attenuated" for w in manifest["warnings"])
    with wave.open(str(ROOT / "tests/fixtures/test-piano-pack/samples/c4.wav")) as sample:
        original = [v[0] for v in struct.iter_unpack("<h", sample.readframes(sample.getnframes()))]
    assert values == [8 * v / 32768 for v in original]
    with wave.open(str(tmp_path / "chord/performance.wav")) as mixed:
        pcm = [v[0] for v in struct.iter_unpack("<h", mixed.readframes(mixed.getnframes()))]
    assert pcm == [round(v * 32768 * manifest["mix"]["gain"]) for v in values]


@pytest.mark.parametrize(
    "fixture",
    ["unsupported-voice.musicxml", "unsupported-backup.musicxml", "unsupported-forward.musicxml"],
)
def test_agent_rejects_inconsistent_polyphonic_cursor_fixtures(fixture: str) -> None:
    result = run("validate", str(ROOT / "tests/fixtures" / fixture))
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["id"] == "musicxml_invalid"


def test_agent_rejects_excessive_event_count_before_render(tmp_path: Path) -> None:
    score = tmp_path / "huge.musicxml"
    score.write_text(
        score_document(
            "<measure><attributes><divisions>1</divisions></attributes>"
            + "<note><rest/><duration>1</duration></note>" * 10001
            + "</measure>"
        )
    )
    result = run("validate", str(score))
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["id"] == "notation_unsupported"
    assert "event limit" in result.stderr.lower()


def test_agent_retains_original_voice_with_complete_performance(tmp_path: Path) -> None:
    store = tmp_path / "packs"
    installed = run(
        "sound-pack", "install", str(ROOT / "tests/fixtures/test-piano-pack"), "--store", str(store)
    )
    assert installed.returncode == 0, installed.stderr
    output = tmp_path / "first"
    result = run(
        "render",
        str(ROOT / "tests/fixtures/renderable-c4.musicxml"),
        "--sound-pack",
        "play-cli-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
        "--output",
        str(output),
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert manifest["schema_version"] == "1.6"
    assert manifest["result_bundle"]["schema_version"] == "1.2"
    voices = manifest["result_bundle"]["voices"]
    assert len(voices) == 1
    voice = voices[0]
    data = (output / voice["relative_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == voice["sha256"]
    assert struct.unpack_from("<HH", data, 20) == (3, 1)
    assert voice["format"] == "IEEE_FLOAT64_LE"
    assert voice["frames"] == manifest["wav"]["frames"]
    assert len(data) == 56 + 8 * voice["frames"]
    assert set(p.name for p in output.iterdir()) == {
        "score.musicxml",
        "validation.json",
        "schedule.json",
        "result.json",
        "performance.wav",
        "voice-0001.wav",
    }
    revision = run(
        "render",
        str(output / "score.musicxml"),
        "--sound-pack",
        "play-cli-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
        "--output",
        str(tmp_path / "second"),
        "--previous-result",
        str(output),
        "--revision-reason",
        "再听一次",
    )
    assert revision.returncode == 0, revision.stderr
    assert (output / voice["relative_path"]).read_bytes() == data


@pytest.mark.parametrize(
    "tamper",
    ["content", "missing", "symlink", "inventory", "voice-path", "null-wav", "rate-overflow"],
)
def test_agent_rejects_corrupt_voice_history_without_replacing_assets(
    tmp_path: Path, tamper: str
) -> None:
    store = tmp_path / "packs"
    assert (
        run(
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/test-piano-pack"),
            "--store",
            str(store),
        ).returncode
        == 0
    )
    options = (
        "--sound-pack",
        "play-cli-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
    )
    first = tmp_path / "first"
    score = ROOT / "tests/fixtures/renderable-c4.musicxml"
    result = run("render", str(score), *options, "--output", str(first))
    assert result.returncode == 0, result.stderr
    original = first / "voice-0001.wav"
    first.chmod(0o755)
    if tamper == "content":
        original.chmod(0o644)
        original.write_bytes(original.read_bytes()[:-8])
        original.chmod(0o444)
    elif tamper in {"missing", "symlink"}:
        original.rename(tmp_path / "retained-original.wav")
        if tamper == "symlink":
            original.symlink_to(tmp_path / "retained-original.wav")
    elif tamper == "inventory":
        (first / "voice-9999.wav").write_bytes(b"unexpected")
        (first / "voice-9999.wav").chmod(0o444)
    else:
        manifest = json.loads((first / "result.json").read_text())
        if tamper == "null-wav":
            manifest["wav"] = None
        elif tamper == "rate-overflow":
            manifest["wav"]["sample_rate_hz"] = 2**64
            manifest["result_bundle"]["voices"][0]["sample_rate_hz"] = 2**64
        else:
            manifest["result_bundle"]["voices"][0]["relative_path"] = "../outside.wav"
        (first / "result.json").chmod(0o644)
        (first / "result.json").write_text(json.dumps(manifest))
        (first / "result.json").chmod(0o444)
    first.chmod(0o555)
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    result = run(
        "render",
        str(score),
        *options,
        "--output",
        str(tmp_path / "second"),
        "--previous-result",
        str(first),
        "--revision-reason",
        "Synthetic feedback",
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["error"]["id"] == "revision_source_invalid"
    assert not (tmp_path / "second").exists()
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before


@pytest.mark.parametrize(
    "middle",
    [
        "<measure><note><rest/><duration>1</duration><voice>1</voice></note></measure>",
        '<measure implicit="yes"><note><rest/><duration>5</duration>'
        "<voice>1</voice></note></measure>",
    ],
)
def test_agent_rejects_ambiguous_measure_spans_in_polyphonic_score(
    tmp_path: Path, middle: str
) -> None:
    full = (
        "<measure><attributes><divisions>1</divisions></attributes>"
        "<note><rest/><duration>4</duration><voice>1</voice></note>"
        "<backup><duration>4</duration></backup>"
        "<note><rest/><duration>4</duration><voice>2</voice></note></measure>"
    )
    score = tmp_path / "invalid-span.musicxml"
    score.write_text(score_document(full + middle + full))
    result = run("validate", str(score))
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["id"] == "musicxml_invalid"


def test_agent_reconstructs_two_distinct_voices_and_revises_only_melody(tmp_path: Path) -> None:
    store = tmp_path / "packs"
    assert (
        run(
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/performance-test-piano-pack"),
            "--store",
            str(store),
        ).returncode
        == 0
    )
    options = (
        "--sound-pack",
        "play-cli-performance-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
    )
    score = ROOT / "tests/fixtures/polyphonic-score/interpretation.musicxml"
    first = tmp_path / "first"
    result = run("render", str(score), *options, "--output", str(first))
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    raw = {}
    for voice in manifest["result_bundle"]["voices"]:
        data = (first / voice["relative_path"]).read_bytes()
        raw[voice["voice"]] = [v[0] for v in struct.iter_unpack("<d", data[56:])]
    assert set(raw) == {"bass", "melody"}
    with wave.open(str(first / "performance.wav")) as wav:
        pcm = [v[0] for v in struct.iter_unpack("<h", wav.readframes(wav.getnframes()))]
    assert pcm == [
        round((bass + melody) * 32768 * manifest["mix"]["gain"])
        for bass, melody in zip(raw["bass"], raw["melody"], strict=True)
    ]
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    working = tmp_path / "revised.musicxml"
    working.write_text(
        score.read_text().replace(
            "<step>C</step><octave>4</octave>",
            "<step>F</step><alter>1</alter><octave>5</octave>",
            1,
        )
    )
    revised = run(
        "render",
        str(working),
        *options,
        "--output",
        str(tmp_path / "second"),
        "--previous-result",
        str(first),
        "--revision-reason",
        "Synthetic feedback: melody first note too low; fixture mutation for revision test.",
    )
    assert revised.returncode == 0, revised.stderr
    assert {p.name: p.read_bytes() for p in first.iterdir()} == before
    assert (tmp_path / "second/voice-0001.wav").read_bytes() == before["voice-0001.wav"]
    assert (tmp_path / "second/voice-0002.wav").read_bytes() != before["voice-0002.wav"]


def test_agent_rejects_voice_storage_budget_before_publishing(tmp_path: Path) -> None:
    score = tmp_path / "large-voices.musicxml"
    music = (
        "<attributes><divisions>1</divisions><time><beats>1000</beats>"
        "<beat-type>4</beat-type></time></attributes>"
    )
    for voice in range(16):
        if voice:
            music += "<backup><duration>1000</duration></backup>"
        music += f"<note><rest/><duration>1000</duration><voice>{voice}</voice></note>"
    score.write_text(score_document("<measure>" + music + "</measure>"))
    store = tmp_path / "packs"
    assert (
        run(
            "sound-pack",
            "install",
            str(ROOT / "tests/fixtures/test-piano-pack"),
            "--store",
            str(store),
        ).returncode
        == 0
    )
    result = run(
        "render",
        str(score),
        "--sound-pack",
        "play-cli-test-piano",
        "--sound-pack-version",
        "1.0.0",
        "--sound-pack-store",
        str(store),
        "--output",
        str(tmp_path / "never-published"),
    )
    assert result.returncode == 3
    assert json.loads(result.stdout)["error"]["id"] == "renderer_resource_limit"
    assert not (tmp_path / "never-published").exists()
    assert not (tmp_path / ".never-published.pending").exists()


def test_agent_rejects_incomplete_measure_in_chord_score(tmp_path: Path) -> None:
    score = tmp_path / "chord-meter.musicxml"
    score.write_text(
        score_document("""
      <measure><attributes><divisions>1</divisions></attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration></note>
      <note><chord/><pitch><step>E</step><octave>4</octave></pitch>
      <duration>4</duration></note></measure>
      <measure><note><rest/><duration>1</duration></note></measure>
      <measure><note><rest/><duration>4</duration></note></measure>""")
    )
    result = run("validate", str(score))
    assert result.returncode != 0
    assert json.loads(result.stdout)["error"]["id"] == "musicxml_invalid"


def test_agent_accepts_triplet_duration_from_time_modification(tmp_path: Path) -> None:
    score = tmp_path / "triplet.musicxml"
    score.write_text(
        score_document(
            """<measure implicit="yes"><attributes><divisions>6</divisions></attributes>
            <note><pitch><step>D</step><octave>4</octave></pitch><duration>2</duration>
              <type>eighth</type><time-modification><actual-notes>3</actual-notes>
              <normal-notes>2</normal-notes></time-modification></note>
            <note><pitch><step>E</step><octave>4</octave></pitch><duration>2</duration>
              <type>eighth</type><time-modification><actual-notes>3</actual-notes>
              <normal-notes>2</normal-notes></time-modification></note>
            <note><pitch><step>F</step><octave>4</octave></pitch><duration>2</duration>
              <type>eighth</type><time-modification><actual-notes>3</actual-notes>
              <normal-notes>2</normal-notes></time-modification></note></measure>"""
        )
    )
    result = run("validate", str(score))
    assert result.returncode == 0, result.stderr
    events = json.loads(result.stdout)["events"]
    assert [event["duration_quarters"] for event in events] == [
        1 / 3,
        1 / 3,
        1 / 3,
    ]

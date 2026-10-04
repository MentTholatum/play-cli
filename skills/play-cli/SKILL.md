---
name: play-cli
description: Perform a complete printed numbered or staff score as piano audio with source coverage evidence, correct it after listening feedback, or validate and render local MusicXML with PLAY CLI.
---

# PLAY CLI

Use this Skill with PLAY CLI 0.10.0. Install its separately supplied wheel in an
isolated Python 3.11+ environment; load the whole `play-cli` Skill directory,
including its Markdown references, into the host Agent. The wheel installs
`playcli`, not the Skill or Standard Piano samples. Version 0.10.0 is a locally
accepted candidate, not a published release.

For feedback about a previously delivered Performance, first follow
[Listening-led correction](listening-correction.md). A request to continue
development, silence, or successful rendering is not Playback Feedback.

For a numbered score image or PDF, first read [Numbered-score interpretation](numbered-score.md).
For staff notation, read [Staff-score interpretation](staff-score.md) instead.
Both printed-score branches must pass the [Source Coverage Gate](source-coverage.md)
before validation or rendering. For local MusicXML, start at validation below.
The Agent owns visual interpretation; PLAY CLI receives only MusicXML.
Use the host's available vision or local PDF viewer. If neither can inspect the
input, report that capability gap and request a readable image; CLI success
cannot substitute for inspecting the source.

Use an explicitly available local Sound Pack. The separately qualified
Standard Piano pack is not bundled with the CLI or Skill. macOS is the formal
MVP platform; Docker and Colima are not runtime requirements.

## Validation handoff

1. Ensure the input is an Agent-prepared local MusicXML file.
2. Run `playcli validate <score.musicxml>`.
3. Request `--schema-version 1.6`, parse the single JSON object from stdout, and check `schema_version` before
   consuming `status`, `score_context`, `events`, `warnings`, or `error`.
4. Treat a non-zero exit status as failure and use the stable `error.id` for
   recovery. Stderr is diagnostic context, not part of the machine contract.

Validation supports pitch, octave, integer accidentals, exact MusicXML
duration/divisions timing, dots, ties, rests, and initial key, meter, and tempo.
The score must have one `part-list` identity matching its single part. Within
the supported subset, singleton cardinality, non-empty semantic values, and
explicitly allowed attributes are validated. This is not complete MusicXML XSD
validation.
Treat each event's `onset_quarters_exact` and `duration_quarters_exact` rational
objects as authoritative. The corresponding numeric fields exist for
compatibility and convenient inspection. One piano part may contain two staves,
multiple explicit voices, chords and backup/forward cursor movements. Voice
identity is independent of hand/staff; retain it across staff changes and ties.
Directions must retain the supported MusicXML child
order and contain at least one `direction-type`.
Standard note positioning, font, color, print, and ID attributes are layout
metadata. Performance-affecting or unknown note attributes return
`notation_unsupported` instead of being silently ignored.
Missing key, meter, and tempo produce the independent `score_default_key`,
`score_default_meter`, and `score_default_tempo` warnings. Do not discard these
warnings. Treat `musicxml_invalid` as a correction request and
`notation_unsupported` as an explicit capability limitation. Expand supported
repeats consistently; simplify musical content only on an explicit Arrangement request.

Do not upload the Playable Score, download assets, claim score recognition is
correct, or imply that validation produced a Performance.

## Performance render handoff

1. Install an audited local pack with
   `playcli sound-pack install <local-pack> --store <pack-store>`.
2. Check it with `playcli sound-pack verify <id> --version <version> --store
   <pack-store>`; `list` returns available identities in stable order.
3. Ensure the verified pack has a compatible PCM16 mono sample for every pitch
   in the normalized schedule.
4. Run `playcli render <score.musicxml> --sound-pack <id>
   --sound-pack-version <version> --sound-pack-store <pack-store> --output
   <bundle>`. Add `--play` only when immediate local playback is desired.
5. Accept success only after all JSON-referenced Bundle artifacts are readable:
   `score.musicxml`, `validation.json`, `schedule.json`, `performance.wav`, and
   `result.json`, plus every declared `result_bundle.voices` WAV.
6. Expect Bundle schema `1.2` and retain its Performance Request history. Playback status is a
   post-publication result in stdout, not part of the persisted Bundle manifest.
   Compare every declared artifact SHA-256 with its local bytes and read all
   PCM frames using a WAV reader: format, rate, channels, frame count and duration
   must match the manifest. Voice WAVs use IEEE float64, not PCM16: check format,
   equal sample rate/full frame count, source/event mapping and declared hashes.
   Resolve each generated relative voice path under the Bundle; no source label is a path.
   Present the complete WAV, links to every original voice WAV and editable MusicXML,
   summarize defaults and interpretation warnings in ordinary language, and
   distinguish successful rendering from recognition accuracy or listening approval.
7. Treat renderer, sample, or Bundle failures as fatal and do not infer partial
   completion.

The renderer follows exact shared onset and duration values, emits rests as
silence, fades deterministic sample truncation, and pads short samples. It must
not add accompaniment, inferred notes, expressive timing, or inferred dynamics.
The open-format score, validation result, schedule, WAV, Score Defaults, Render
Warnings, and provenance are assembled together through no-follow descriptors.
Before publication, the inventory must be exactly the five core files plus
the declared voice WAVs, and every
file must remain the same single-link inode created by this render. All files
are completely re-read and validated, sealed as `0444`, and the directory as
`0555`; the same descriptors then repeat exact inventory, content, mode,
identity, link-count, hash, and WAV frame-byte verification. A single
atomic no-replace directory rename publishes the sealed Bundle, followed only
by a public parent/Bundle inode rebind.
Treat the delivered Bundle as a read-only snapshot. Copy `score.musicxml` into
a new revision rather than changing historical output in place. The seal blocks
ordinary concurrent mutations, not deliberate owner `chmod` or a writable
descriptor retained before sealing.
An existing output or deterministic pending attempt requires a new output path
or manual inspection; PLAY CLI never overwrites it. Renderer preflight failures
occur before pending Bundle creation. A Performance is limited to 600 seconds
and 28,800,000 mono frames, covering ten minutes through 48 kHz while bounding
allocation from extreme tempo or sample-rate metadata. Allocation or encoding
exhaustion returns `renderer_resource_unavailable` without publishing a Bundle.
A note shorter than 160 frames at the selected sample rate returns
`renderer_timing_unrepresentable` rather than a silent Performance.

Each notated voice, including chords and all-rest voices, has one retained
pre-mix WAV starting at time zero and padded through the entire score. Do not
trim or independently normalize these originals. Values above full scale are
preserved in float64; final PCM16 mixing alone applies a disclosed global gain.
Raw voices may exceed a player's playback range; use the complete Performance
for normal listening and preserve originals for analysis/re-mixing.
Current limits are 8 MiB MusicXML, 10,000 note/forward events, 64 simultaneous
notes, 16 retained voices and 512 MiB total audio output. Old five-file Bundles
remain readable revision parents; historical voice files are not fabricated.

The test Sound Pack proves integration only. Do not present it as the Standard
Piano Sound Pack or silently obtain another Sound Pack from the network. When
`sound_pack_missing` is returned, fill the declared `local_source` input in the
structured install `argv_template`; the placeholder is not itself an executable
shell command. Treat `sound_pack_corrupt` and `sound_pack_conflict` as fatal and
never bypass lifecycle verification with a raw directory path.
Canonicalize `<pack-store>` before use: PLAY CLI rejects every existing symlink
component in store and installed-pack paths. A manifest must pin its source as
a URI with an explicit scheme plus a fixed numeric dotted `version` or
hexadecimal `commit`, and declare a hashed, complete UTF-8 license text file.
Installed manifests, licenses, receipts, and samples are read without following
symlinks; concurrent same-content installs remain idempotent. The installer
uses the native macOS/Linux atomic no-replace primitive and fails closed if it
is unavailable, then validates the exact installed directory handles before
reporting success. The hidden staging directory becomes the final version
directory directly. A per-version persistent lock and deterministic pending
name bound failure growth, while ordinary idempotent reinstalls do not create
staging. The receipt is written with no-replace semantics only after anchored
post-rename validation and is the final ready marker. Missing or mismatched
receipts are never verified or rendered. Before returning success the installer
reopens the public identity/version and confirms their pre-bound inodes;
mismatches fail without cleanup. Failures may leave one ignored hidden
staging directory or an uncommitted final directory for manual inspection
because PLAY CLI never recursively deletes a possibly replaced pathname. Store
creation and list are component-wise and no-follow.
Same-content receipt repair and the already-installed branch operate through
the version descriptor opened from the locked identity, then require the same
public identity/version inode confirmation. If the public binding changes after
receipt commit, installation fails without cleanup; a committed victim may
remain as a non-public orphan rather than risk changing a replacement pack.

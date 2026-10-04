# Numbered Study No. 1

The default-context variant uses `source-defaults.svg(.png)` and
`interpretation-defaults.musicxml`.
The Agent viewed this complete header-free variant; its key, meter and tempo
are all absent. Preserve the same notes/repeats while disclosing all three CLI
defaults plus the existing absolute-register interpretation. Its WAV must
equal the original numbered fixture; only metadata/default provenance differs.

Original printed monophonic exercise created by PLAY CLI contributors on
2026-09-12 and dedicated to the public domain under CC0-1.0. This dedication
covers the SVG, rendered PNG and MusicXML transcription in this directory.
No external melody or sample recording is included.

The Agent visually inspected the complete rendered source before authoring
MusicXML. This is a repository-authored, known-answer workflow fixture, not a
blind OCR benchmark or evidence of arbitrary score recognition quality.

Source measures 1 and 2 repeat once; source measure 3 follows. Expanded mapping:
1→1,3; 2→2,4; 3→5. Key is 1=C and meter is 4/4. Tempo is absent: retain the CLI's
100 BPM warning. Absolute register is unspecified: the Agent's C4 tonic choice
is retained as an interpretation uncertainty, not claimed as printed notation.

Expected normalized (MIDI, quarter duration) per source measure:

- 1: (60,1), (62,1.5), (64,0.5), (rest,1).
- 2: (66,0.5), (67,0.5), (72,3); the two printed upper 1s are tied.
- 3: (60,1), (59,1), (60,2).

After repeat expansion: 17 events, 20 quarter beats, 12 seconds at 100 BPM.
The uncertainty and default are expected; successful WAV generation is not a
human listening acceptance.

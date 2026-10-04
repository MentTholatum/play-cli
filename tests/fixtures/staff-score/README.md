# Staff Study No. 1

Original printed monophonic exercise by PLAY CLI contributors, 2026-09-12.
The SVG, rendered PNG and MusicXML transcription are dedicated to the public
domain under CC0-1.0. No external composition or sample recording is included.

The Agent inspected the complete rendered PNG before writing the MusicXML.
This is a repository-authored known-answer workflow fixture, not a blind OMR
benchmark. The visual source and editable transcription are separate artifacts.

The treble-clef score has one sharp (G major), 3/4 meter and quarter = 120 BPM.
Source measures 1 and 2 repeat once; measures 3 and 4 follow. Mapping:
1→1,3; 2→2,4; 3→5; 4→6. Expected (MIDI, quarter duration):

- 1: (66,0.5), (67,0.5), (69,1.5), (71,0.5).
- 2: (72,2), (rest,1); the two C5 quarter notes are tied.
- 3: (65,1), (65,1), (67,1); the natural carries to the second F4.
- 4: (66,3); the barline restores F-sharp, and the half note is dotted.

Expanded result: 16 normalized events, 18 quarter beats, 9 seconds.
Both rest intervals are silent: 2.5–3 seconds and 5.5–6 seconds.
No context defaults or interpretation uncertainty are expected for this clear
reading. Human listening is a separate acceptance step.

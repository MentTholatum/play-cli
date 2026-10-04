# Licensing scope

The Apache License 2.0 in `LICENSE` applies to PLAY CLI source code, tests,
documentation, and the official Skill unless a file states otherwise.

It does not apply to Sound Packs, audio samples, or assets derived from audio
samples. Those materials must remain separately installable and carry their own
source, version, integrity hash, license, attribution, and modification record.
No production Sound Pack or raw audio sample is included in this repository.
User-provided score images and private demonstration performances are excluded
from this public source repository.
The Standard Piano candidate is a separately built derivative of Salamander
Grand Piano V3 under CC BY 3.0. Its source pin, upstream license hash,
attribution and audio-free qualification records are in `standards/`;
the transformation is implemented in `tools/build_salamander_standard_piano_pack.py`.
The derived pack must ship
with the complete upstream `LICENSE`, `SOURCE.md`, and `SOURCE-RECEIPT.json`.
Distribution remains unapproved until the recorded human listening gate passes.
`tests/fixtures/test-piano-pack/` is a development-only Sound Pack.
Its generated C4 audio sample and associated fixture materials are independently
released under CC0-1.0. That directory contains the complete offline CC0 1.0
legal code in `LICENSE`; `SOURCE.md` records its official source and fixture
provenance, and manifest schema `1.2` pins its typed source revision plus the
complete license text and sample integrity hashes.

`tests/fixtures/performance-test-piano-pack/` is a separate development-only
Sound Pack for the complete supported monophonic fixture. Its deterministic
MIDI 56, 60, and 78 samples, generator, and metadata are independently released
under CC0-1.0. Its own `LICENSE`, `SOURCE.md`, and manifest retain the complete
offline legal text, source pin, generation command, sizes, formats, and SHA-256
hashes. Neither test pack is a production piano library.

# Test Piano Sound Pack provenance

- Source: deterministic output of `generate_sample.py` in this directory, identified
  by the auditable URI `urn:play-cli:test-piano-generator`
- Source pin: manifest `source.revision` type `version`, value `1.1`
- Creator and attribution: PLAY CLI contributors
- License: CC0-1.0; the complete offline legal code is in `LICENSE`
- License source: https://creativecommons.org/publicdomain/zero/1.0/legalcode.txt
- Recording source: none; the waveform is generated from decaying harmonics
- Release: deterministic 320-frame integer linear fade followed by 800 silent frames
- Modifications: none after generation
- Intended use: subprocess tests and local development only
- Pack manifest schema: `1.2`; the license declaration pins the full text hash

Run `.venv/bin/python tests/fixtures/test-piano-pack/generate_sample.py` to
reproduce `samples/c4.wav`. The expected SHA-256 is pinned in `manifest.json`.

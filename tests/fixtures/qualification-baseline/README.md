# Self-contained qualification fixture

`source/` contains only the Apache-2.0 PLAY CLI 0.7.0 runtime sources,
license, and minimal test-only packaging metadata. No Git history or historical
project documents are required. The runtime source inventory retains SHA-256
`2315a3d602b7d8a24a6a63661a1c68b684300be5107ac39cdacf4370ff56f715`.

Tests build this source with setuptools 84.0.0 and `SOURCE_DATE_EPOCH=1789142400`.
`wheel.lock.json` pins the resulting test wheel and its complete inventory.
`tests/qualification_baseline.py` loads an isolated copy of the real qualifier
and changes only its authority path and authority-file digest. All wheel,
source, runtime, provenance, and audio checks remain active.

The test wheel is NOT the historical approved production wheel. The production
qualifier and the records in `standards/` remain unchanged and reject this test
wheel. These fixtures establish regression behavior, not production listening
approval or a new qualification record.

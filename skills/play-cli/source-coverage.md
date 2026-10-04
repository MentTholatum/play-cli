# Source Coverage Gate

Use this gate for every image or PDF performance request. It separates complete
source transcription from valid MusicXML rendering.

## Declare the scope

Default `requested_scope` to `complete`: every musical measure visible in the
submitted source, in performance order. Use `excerpt` only when the user names a
source range, and `arrangement` only when the user asks to simplify or rewrite
the music. A request containing only “演奏” or “play” has complete scope.

Create `coverage.json` beside the working MusicXML before transcription. Record:

```json
{
  "schema_version": "1.0",
  "requested_scope": "complete",
  "source": {"path": "/absolute/source", "sha256": "..."},
  "music_pages": [1],
  "source_measure_count": 22,
  "covered_measure_count": 22,
  "unresolved_measure_count": 0,
  "measures": [
    {
      "source": "page:1/system:1/measure:1",
      "output_measures": [1],
      "status": "verified"
    }
  ]
}
```

For unnumbered music, assign stable positions in page, system and left-to-right
measure order. Count performed occurrences after expanding repeats; retain the
source-to-output mapping rather than replacing the source count with the expanded
count.

## Verify every measure

For each measure entry, reopen its source region and compare every staff, voice,
note, rest, chord, accidental, tie and performed duration with the MusicXML.
Set `status` to `verified` only after that comparison. Record a readable symbol
or unsupported notation as `unresolved`, with its exact source position and
reason.

The gate passes only when all of these statements are true:

- `covered_measure_count` equals `source_measure_count` for the requested range.
- `unresolved_measure_count` is zero.
- Every source measure appears exactly once in `measures` and has `verified`
  status.
- Every performed output measure is referenced by the source-to-output mapping.
- The first and last covered positions match the declared request scope.

If any statement is false, continue transcription or report the exact unresolved
positions. Enter validation and rendering only after the gate passes. A valid
MusicXML response, playable WAV or pleasant listening result cannot change the
coverage status.

## Deliver the scope

For complete scope, describe the result as complete only when the gate passes.
For an explicit excerpt or arrangement, include that scope in the MusicXML title,
output directory name and delivery message. Always deliver `coverage.json` with
the MusicXML and audio links so another Agent can reproduce the completion check.

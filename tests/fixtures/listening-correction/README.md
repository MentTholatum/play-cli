# Listening correction fixture

This is a synthetic development scenario, not feedback from the actual user.
The source is the unchanged CC0 image in `../numbered-score/source.svg.png`.
The Agent reopened that image and inspected the final printed phrase:
the 7 has a lower octave dot, so its pitch is B3 under the retained C4 tonic.

`initial.musicxml` deliberately seeds one error: that note is B4.
`feedback.json` explicitly labels “后面听起来不对” as synthetic feedback referring
to delivered revision 1. `revised.musicxml` corrects only B4→B3 and retains the
feedback identifier and source-grounded reason as descriptive XML metadata.
The overall-register uncertainty and default 100 BPM remain unchanged.

Both scores contain 17 events lasting 12 seconds. Event index 15 (zero-based),
onset 17 quarter beats / 10.2 seconds, changes MIDI 71→59; every other event is
identical. No preview is necessary because the source dot is clear.

These derivative fixture scores are dedicated to the public domain under
CC0-1.0, like the original exercise. They are synthetic test inputs, not
previously delivered user performances.

# Quiz generation time budgets

Calibrated on 30 October 2026 using the previous 30 days of production telemetry:
151 successful generation records, 114 matched to size/count events. The other
37 records had no size metadata. No student content or identifiers are included
here. The measurements represent the full AI generation stage, not individual
provider attempts; a first-ranked winning model can still have retried keys.

The most comparable small-input cohort contains 50 single-image, regular-path
requests for 1–10 questions, with a first-ranked winner and exactly the requested
number of questions. Of these, 39 finished within 50 seconds, 46 within 120, and
48 within 180. These are historical success durations, not counterfactual failure
rates for new timeout settings.

For general subject content (35 records in that cohort), P90 was 77.09s and P95
111.68s; 33 finished within 120s. For math (13 records), P90 was 160.23s; 12
finished within 180s. These support more conservative limits than the previous
50s small-input attempt allowance. They do not establish optimal attempt limits.

## Limits

For up to 10 questions of general subject content:

| Pages or images | Attempt limit | Shared AI stage budget |
| --- | ---: | ---: |
| 1 | 120s | 240s |
| 2–5 | 150s | 300s |
| 6–15 | 180s | 360s |
| 16–35 | 240s | 480s |
| Above 35 | 300s | 600s |

Math raises the attempt minimum to 180s for up to 5 source items, or 240s above
5 items. Output size raises the minimum to 180s for 11–30 questions, 240s for
31–60, and 300s for 61–120. Use the maximum of source, math and output limits;
do not sum them. The shared total is twice that allowance, at most 600s. This
does not guarantee two complete provider attempts: preparation and continuations
also consume the shared allowance. The existing administrative timeout remains
an upper bound on each attempt.

Only 5 exact-count, first-ranked, regular-path successes had 2–5 source items;
7 had 6–15, 2 had 16–35, and none had above 35. These larger-input and
output-size tiers are conservative operating choices supported by sparse data,
not independently measured P95 thresholds. The single-page PDF rule borrows the
image allowance; the equivalent-page count for text remains word-based.

Parallel parts, fallback models and continuation requests share one deadline.
Budget exhaustion retains the source/count and refunds the charge for retry.
Detection, audio transcription, upload/queue waiting, persistence and rendering
are outside this AI stage budget. Cooperative cleanup may add a short grace
period. Successful requests finish immediately; these values are ceilings, not
delays. Complete per-attempt success/failure telemetry is needed to recalibrate
more precisely, particularly for sparse cohorts.

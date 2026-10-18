# Classification isolation between quiz inputs

Completed quizzes retain FSM data for review and replay. New media/text inputs
previously merged their fields into that data. A PDF uploaded after a text quiz
could therefore carry both the new `file_paths`/`file_hash` and the previous
`pure_text`. Detection preferred the old text and cached its classification under
the new file hash. Because classification results are shared for identical file
bytes, another student uploading that PDF would receive the same wrong result.
The reverse sequence could attach a previous file hash to new text or an audio
transcript, contaminating that file's classification and quiz cache.

Validated new inputs now replace FSM data rather than merging it. Media detection
ignores retained direct text, and the classifier prioritizes explicit media paths.
The same reset applies to media cache-choice screens and transcript-to-quiz
conversion. Existing active-quiz transition confirmation still runs before intake.
No points, results, leaderboard rules or persisted user records are changed.

Redis classification keys now use `subject_classify:v3:`. Existing v2 results are
ignored and expire normally; no Redis flush or database cleanup is required.
Unconfirmed classifications will be recomputed once per file as the new cache fills.
Community-confirmed classification locks, votes and previously saved quizzes are
preserved. If a specific historical lock or saved quiz is reported as incorrect,
inspect that record separately before changing it; the namespace change only
invalidates temporary classification results.

`SUBJECT_CLASSIFICATION` logs distinguish `media`, `text`, `redis` and
`community_lock` sources without logging content or user information.

Regression coverage: completed text → PDF → second account, media → text,
photo intake, office text at generation, cache-choice reset, transcript conversion,
retained dirty state, old Redis results, unavailable detection, community locks
and preservation of an active quiz awaiting transition confirmation.

Telegram smoke check on the test bot: finish a text quiz, upload an unrelated
PDF, then upload the same PDF from another test account. Repeat PDF → text and
photo → text. Confirm each subject against the current content and check that the
second upload uses the newly computed classification. Do not bulk-delete saved
quizzes or classification votes as part of this check.

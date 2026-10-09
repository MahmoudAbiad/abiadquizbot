# Compact quiz export on a 512 MB worker

PDF and Word export now offer student (questions only) and teacher (questions plus
answers/explanations) variants. Existing callback buttons still select teacher
output. Mathematical content uses a separate compact print layout with the
existing mathtext/Arabic engine, no browser or external rendering service.

- Options use four/two columns only when measured content fits; otherwise stacked.
- Pages contain as many readable question blocks as fit, not a fixed quota.
- Long text continues in bounded blocks. Oversized indivisible tables/matrices or
  unsupported formulas fail explicitly rather than clipping or changing meaning.
- Answers retain rendered fractions and formulas. Plain-text fraction conversion
  also groups numerator and denominator separately.
- Current student/teacher Word exports still embed mathematical blocks as images;
  formulas are not editable native Word equations.

## Admission and memory

`services.processing_limits.HEAVY_PROCESSING_SEMAPHORE` is the same two-slot
admission gate shared by quiz generation and exports. The old quiz-service private
name remains an alias. Exports acquire EXPORT_SLOT first, then the shared gate,
then the existing RENDER_SEMAPHORE. One export builds/uploads at a time. The export
slot covers upload too, preventing multiple complete files waiting in memory.

There are at most eight admitted export requests (including the running one),
one per user. Additional requests receive an explicit busy response. Pending
exports do not acquire heavy slots. Cancellation waits for the actual worker
thread before releasing permits. Cleanup runs on success, failure and cancellation.

One bounded print canvas (1500 x at most 1750 pixels) is rendered at a time;
image resources are released between blocks. PDF/Word libraries still retain
compressed document resources until final serialization. Therefore exports are
limited to 200 questions and 500,000 characters of input representation, with a
40,000-character limit per question. These guards do not guarantee total bot RSS.

All limits are per Python process and transient; waiting requests do not survive
restart. Run one bot worker per 512 MB container; multiple processes each have
their own permits. No Redis/database schema/security changes are required.

## Validation

- Print regression tests: student/teacher PDF and Word, fraction grouping,
  long-question continuation, tables/matrices and explicit invalid-math failure.
- Queue tests: saturated shared gate, duplicate admission, upload serialization,
  full queue, failures, repeated cancellation and callback compatibility/length.
- Existing Telegram image-layout tests remain passing.
- Visual QA of generated Arabic question/solution PDFs with repository fonts.
- Local 200-question teacher PDF stress run: 132.1 MiB peak process RSS,
  9.21 MiB PDF, 42.4 seconds. This isolated measurement excludes a live bot's
  clients, sessions, other workloads and deployment differences.
- A synthetic 13-short-question fixture occupies three student pages and six
  teacher pages. This is not a reconstruction of the user's uploaded quiz.

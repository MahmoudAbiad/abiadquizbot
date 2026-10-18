# Funded wallet rejected at the zero boundary

A legacy `users.free_points` balance of `2.6645352591003757e-15` is
positive but displays as `0.00`. `wallet_debit` selected a numeric deduction
using that balance, then subtracted it from the original double-precision column.
PostgreSQL converted the deduction back to double precision for that subtraction,
producing `-4.33873497871556e-30`. The nonnegative balance constraint aborted the
whole transaction, even though paid points covered the cost. No debit receipt or
partial balance update survived that rejection.

The replacement function subtracts from `u.free_points::numeric` and
`u.paid_points::numeric`, matching its existing pre-check, allocation and receipt
arithmetic. The assignment into the existing column is the only conversion back
to double precision. This migration replaces a function only: no balance rows,
column types, prices, charge priority, refund rules or execute privileges change.

Previously `update_user_stats` returned `None` for both a confirmed rejection and
an RPC exception. It now raises `WalletDebitError` for RPC/response errors; only an
explicit null RPC result remains a rejection. Quiz generation preserves its input
on a technical debit failure and does not start AI generation or issue a refund.
Cached quizzes also show a technical error instead of requesting a recharge.
Unconfirmed outcomes are never automatically retried. Audio's existing exception
handler likewise receives a technical failure instead of a balance rejection.
The low-balance UI also checks for a sufficient displayed balance before showing
recharge instructions or recording an insufficient-balance event.

Tests reproduce the original constraint error with real double-precision columns,
then cover an exact depletion of free/paid/gift buckets, fractional amounts,
genuine insufficient funds, atomic receipts, idempotent refunds and RPC failures.
CI runs both the complete gift-wallet SQL scenarios and decimal regressions in
the PostgreSQL 16 admin/concurrency suite.

Apply `20261018093843_wallet_debit_numeric_arithmetic.sql` to the named test
project only for this rollout. No data cleanup is needed. Verification can use a
synthetic user inside a transaction that is rolled back, without charging a real
student. A Telegram smoke test should then confirm that a funded student can
generate a quiz and that insufficient funds still report the actual deficit.

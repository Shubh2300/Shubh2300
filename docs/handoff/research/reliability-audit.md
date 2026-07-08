# Message/Call Reliability Audit — findings from `ai-phone-intake` (fork/aiops-pipeline)

Read-only investigation of the real, live-at-the-office `ai-phone-intake` codebase
(`shubh2300/ai-phone-intake`, branch `fork/aiops-pipeline`). This is load-bearing
engineering input for the Back Office platform, not background reading — it
tells us exactly what NOT to repeat when we build the equivalent subsystems.

## The core question

Can a message, call, or notification be silently missed — and what mechanism
(if any) catches it?

## Finding 1 — Vapi phone intake: no reconciliation exists (most serious)

Call intake is **100% webhook-dependent**. The only code path that creates a
call record is the `/webhooks/vapi` handler. `vapi_backfill.py` — despite the
name — only fills in *missing fields on rows already in the DB*; it never asks
Vapi "what calls did you actually handle?" and diffs that against local state.
No scheduler runs it automatically either (manual admin endpoint only).

**Concrete failure:** if the app is down (deploy, crash, restart) for N minutes
when a call ends, that call is gone. Permanently. No log, no flag, no way for
the system to ever notice — someone would have to manually compare Vapi's
dashboard count to ours.

**Back Office requirement:** any webhook-fed intake (Vapi calls, EMR events,
RingCentral messages) needs a periodic job that pulls the *source system's own
record* and diffs it against local state — not just "backfill fields on rows
we already have." This is exactly what Temporal recurring workflows are good
for. Build it before go-live; nothing in ai-phone-intake does this today.

## Finding 2 — RingCentral SMS: one tier better than Vapi, still has a real gap

**Fax has it right:** an independent scheduled poll (every 15 min) that
doesn't depend on any webhook firing — real reconciliation.

**SMS does not.** It only re-syncs *reactively*, inside the webhook handler
itself, when a webhook fires. If the webhook never fires (subscription
lapsed), nothing catches it. Webhook-subscription renewal runs every 12h but
failures are **logged, not alerted** — a silent multi-hour or multi-day outage
of inbound SMS is possible and nothing surfaces it to staff.

**Separately:** outbound messages are marked "sent" the instant RingCentral's
API *accepts* the request — never reconciled against actual delivery status.
Staff can see something as "handled" that never delivered.

**Back Office requirement:** copy the fax pattern (independent scheduled poll)
for every channel, not the SMS pattern. Escalate subscription-renewal failures
past a log line — treat "external subscription broke N times" as page-worthy.
Reconcile local "sent" status against the provider's real delivery record.

## Finding 3 — Inbox "handled" status is trusted local state, not verified truth

`inbox.classify_status()` / `reply_owed()` and `outbound_drafts.cared_items()`
are pure functions over local DB fields — none of them call back to
RingCentral/Vapi to confirm reality at read time. The system's only defense
is the delivery-status webhook (Finding 2's gap), so this inherits everything
above.

## Positive finding worth keeping: the outbound-call compliance gate

Placing an outbound call is NOT a simple "approved → send." It requires, in
order: `OUTBOUND_CALLS_ENABLED` kill switch → explicit `compliance_confirmed`
field (a human attests compliance, not just clicks approve) → programmatic
do-not-call check → office-hours check → only then Vapi placement. This
multi-gate pattern is exactly right and should be the template for any
Back Office write action that's regulated/risky, not just EMR writes.

Currently `OUTBOUND_CALLS_ENABLED=False` — code-complete but never live-tested
with a real call, per the repo's own docs.

## Bottom line for the Back Office build

1. Every webhook-fed ingestion needs source-of-truth reconciliation, not just
   local-row backfill. No exceptions.
2. Subscription/webhook health failures must escalate, not just log.
3. "Sent"/"handled" status must be reconciled against the provider's real
   state, not trusted from the moment we made the API call.
4. The outbound-call compliance-gate shape (kill switch + explicit human
   attestation + automated checks + then execute) is the right template for
   pre-op/post-op/recall calls and any other regulated patient contact.

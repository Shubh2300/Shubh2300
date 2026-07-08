# Prior Art — the "Atlantic Hub" fork discovery

Found inside `shubh2300/ai-phone-intake`, branch `fork/aiops-pipeline`. This is
a previous, unfinished attempt at almost exactly what the Back Office platform
is now building — worth knowing about so we don't rediscover the same lessons
the hard way, and don't accidentally think it's further along than it is.

## What it was trying to build

Design spec: `docs/superpowers/specs/2026-07-02-atlantic-hub-integration-design.md`
(inside that repo). Goal: converge `n8n-office`'s approval-card engine (chat
agent, human-approval cards, real Svigg/SIS booking-cancel-reschedule writes)
INTO `ai-phone-intake`, as one unified operational hub — proposal-builder →
approvals-queue → pre-check → execute → post-verify, backed by a
"verified-truth" provenance layer (every field tagged source/verified/as_of,
never a fabricated value).

This is the same shape as the Back Office platform, arrived at independently.

## How far it got — real, but a local prototype, not infrastructure

**Real, working code, not stubs:**
- `app/hub_agent.py` (1,241 lines) — 12 read tools + 6 write tools that only
  *enqueue* approval cards, never execute directly.
- `app/approval_execute.py` — genuinely good discipline: never fabricates a
  success. SMS/email sends are real but hard-gated behind an explicit
  allowlist (empty by default = blocks everything). EMR writes gated behind
  a master flag (default off), returning an honest `"ready_gated"` instead of
  faking success while off.
- `emr_read_sync.py` did real live pulls against SIS, then correctly
  repointed schedule sync to Svigg after discovering SIS-only coverage was
  wrong (7 stale rows vs. 51 real ones) — real debugging against live
  systems, not documentation.

**Why it's not further along than the current build, despite being real:**
- Its datastore (`aiops.db`, SQLite) is gitignored and not reproducible — the
  "867 rows" cited in commit messages was a one-time snapshot on one laptop.
- `emr_bridge.py` shells out to a **hardcoded absolute path**
  (`/Users/shubh/n8n-office/python/integrations`) — cannot run anywhere else.
- Zero automated tests on any fork-added module.
- No workflow engine, no durable execution, single-process SQLite.

## What's worth learning from vs. discarding entirely

Nothing here gets copied in. This whole fork is 1.0 — read it to know what's
already proven to work and what already failed, then build the equivalent
fresh. Treat it the way you'd study a torn-down engine before machining new
parts, not a bin of parts to bolt on.

**Patterns proven to work (reimplement fresh — the files themselves are not
the deliverable):**
- The design spec's non-negotiables: verified-truth/provenance tagging,
  human-in-the-loop signature via initials, honest-on-writes (no fabricated
  success), `pre_check → execute → post-verify`.
- The `approval_execute.py` gating shape: master flag + allowlist + honest
  "ready_gated" no-op when off. Rebuild this exact shape as new code in the
  Back Office Action Registry — don't import the file.
- Concrete EMR-write facts learned the hard way, worth knowing before
  rebuilding the bridge: the `bk_p` booking field names were never in a HAR
  and needed live discovery; the SIS→Svigg schedule-source correction; the
  single-serialized-browser-session constraint for SIS/Svigg probes (only
  one Playwright session can safely drive either EMR at a time — matters for
  how the new Bridge gets architected, even though it's built from scratch).

**Discard entirely, including as reference:**
- The actual `aiops.db` schema/data.
- The hardcoded-path `emr_bridge.py` subprocess shim.
- Its RingCentral poll-only lane as code (untested, never scheduled) — the
  *idea* (independent poll, not webhook-reactive) is right; see
  `reliability-audit.md`. Build a new implementation of that idea against
  Postgres/Temporal — the old module isn't a starting point.

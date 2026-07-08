# Phase 1 build log — what actually happened

This is a history of the first build pass, kept for the engineering lessons,
not as a spec to follow. Under the 2.0 "nothing gets copied over" rule, the
*code* from this pass may get rewritten — but the process discipline below
(multi-agent coordination, catching real bugs, verifying instead of trusting)
should carry forward regardless of what survives.

## How it was built

Three agents ran in parallel, each owning a disjoint slice of `back-office/`:
1. Postgres schema + Action Registry + local EMR Bridge (vendored SIS/Svigg
   clients under the old "toolbox" framing).
2. FastAPI backend (parser → registry validation → approval queue → Temporal
   handoff) + Temporal worker + docker-compose.
3. Next.js dashboard (tasks/approvals/runs/audit/patients pages).

They ran without seeing each other's final output. That's the point of
parallelizing — and also exactly why the next section happened.

## The bug that mattered most: a real schema mismatch, not a typo

Agent 1's `db/schema.sql` modeled `approvals` as a **child** of `action_runs`
— an action_run has to exist first, then an approval references it via
`action_run_id`, with two *different* status enums (`run_status` on
action_runs, a narrower `approval_status` on approvals).

Agent 2's `apps/api/repositories.py` and `schemas.py` were written against an
**imagined, flatter, inverted** shape: `approvals.action`, `approvals.intent`,
`approvals.state`, `action_runs.approval_id`, `action_runs.workflow_id`,
`action_runs.warnings` — none of which exist in the real schema, and the
relationship pointed the wrong way.

This wasn't caught by either agent — it was caught by the **third** agent
(building the web dashboard), which read both `schemas.py` and `db/schema.sql`
while grounding its API client, and flagged the disagreement in its own
completion report instead of silently guessing. That flag is what triggered
a dedicated reconciliation pass.

**Lesson:** when parallel agents build against a shared contract, the contract
itself (the schema) has to be read by whoever integrates last, or nobody
notices two people built against two different mental models of the same
tables. A "did the report claim something concerning?" check on every
sub-agent's return is worth the read.

## The reconciliation agent found MORE bugs than the diagnosis

A dedicated agent was sent to fix the schema mismatch. While doing that, it
also found and fixed real bugs the mismatch diagnosis hadn't surfaced:
- `worker/workflows.py` read `validated["inputs"]` but the validator actually
  returned the key `"payload"` — a guaranteed `KeyError` on every workflow run.
- The risk≥2 write-approval token was never actually minted/checked end to
  end — the API never generated one, the worker never enforced it. The write
  gate existed in the spec but not in running code.
- `action_runs` route prefix mismatch (`/action-runs` vs. what the web called,
  `/action_runs`) — would have 404'd.
- `GET /approvals` didn't exist; only `/approvals/pending` did, but the web
  called the plain list route.

**Lesson:** a "fix the known bug" agent should be told to verify the
surrounding contract, not just patch the one named issue — the same
under-specified-interface problem tends to produce more than one symptom.

## The collision nobody planned for

While the dedicated reconciliation agent was mid-flight, the **original**
backend agent (from the first parallel batch) turned out to still be running
in the background and had *independently* noticed and started fixing the
same schema mismatch — meaning two agents were editing overlapping files at
the same time, unsupervised.

This was caught before any damage, by checking `py_compile` + the actual test
suite (21/21 passing) against the final on-disk state, rather than trusting
either agent's self-report. No corruption occurred, but it could have.

**Lesson:** background agents don't announce when they're still alive. Before
trusting "agent X says it's done," check what's actually on disk and whether
anything else might have touched the same files. Verify against the real
file state and a real test run, every time — never take a completion report
as sufficient proof on its own.

## Verification pattern used throughout (worth repeating)

At every commit point: `python3 -m py_compile` on every touched file,
`pytest` on the pure-logic test suite (21 tests, no live DB required — the
service layer is tested behind an in-memory store, not mocked EMR data),
`npx tsc --noEmit` on the web app. Nothing was marked done on an agent's
word alone — every claim was independently re-checked before committing.

The Action Registry was also validated against a **real, live Postgres 16**
instance (not just eyeballed): the audit log's hash-chain trigger was
exercised (insert → verify chain), and `UPDATE`/`DELETE` attempts against
`audit_logs` were confirmed blocked by the append-only trigger.

## Repos discovered and added mid-build (why they matter)

- **`n8n-office-mirror`** — the *real*, current production Back Office code
  (`mainlinesurgery-a11y/n8n-office`, branch `backoffice-autopilot-live-20260705`)
  turned out to be far more complete than what was already in this repo (a
  stale snapshot): a Koko chat agent, real approval-engine executors,
  dual-EMR billing, Drive chart-folder templates. Found via a secrets/backup
  doc the owner uploaded that referenced the real repo and branch by name.
- **`ai-phone-intake`** — first upload was the wrong folder entirely (turned
  out to be a zip of *this session's own repo*, not ai-phone-intake). The
  real one needed a local Claude Code session (with actual filesystem access
  to the owner's Mac) to push a full `git push --mirror` to a new GitHub repo
  under the owner's account, since this session can't reach local disks.
- **`koko-intake`** — same local-session mirroring pattern, much smaller,
  early-stage (P0/P1 only — voice and PDF output not yet built).

**Lesson:** always verify an uploaded zip is actually what it claims to be
before analyzing it (`git remote -v`, `git log -1` caught the wrong-folder
upload immediately) — don't assume the filename is accurate.

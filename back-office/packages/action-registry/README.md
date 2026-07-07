# Action Registry

The Action Registry is the **single source of truth** for what the Back Office
platform is allowed to do to an EMR. The AI never invents an action: it may only
propose one of the actions defined here, and the bridge will only execute an
action whose contract says it is genuinely implemented.

## Files

| File | Purpose |
|---|---|
| `actions.json` | One contract per approved action (24 total). The DB table `action_registry` is a projection of this file. |
| `action_intent.schema.json` | Strict JSON Schema (`additionalProperties: false`) for a single parsed staff request mapped onto exactly one action. |
| `README.md` | This document. |

## The two-phase, verified model

Every action runs through the same lifecycle (see `DECISIONS.md`):

```
staff request
  -> parse into an ActionIntent (validated against action_intent.schema.json)
  -> look up the contract in actions.json
  -> [writes] enqueue an approval; a human approves on screen
  -> bridge executes deterministically (Playwright only, no AI at the wheel)
  -> post-action verification: re-read the EMR + screenshot proof
  -> append a hash-chained audit_logs row
```

Reads (risk_level 1) skip the approval step. Writes (risk_level >= 2) require an
approval token that the bridge checks **at the point of the destructive call**.

## Contract fields (per action)

| Field | Meaning |
|---|---|
| `action_name` | Unique key; matches the enum in the intent schema. |
| `target_system` | `sis` \| `svigg` \| `both` \| `unknown`. |
| `risk_level` | `1` read, `2` scheduling, `3` patient, `4` notes. |
| `requires_approval` | True for all writes. |
| `required_inputs` | Inputs the caller must supply. |
| `preconditions` | What must be true before execution (auth, allowlist, token). |
| `execution_steps` | Outline of the deterministic steps (or the block reason). |
| `selectors_needed` | DOM selectors / REST routes the step depends on. `UNKNOWN` where unmapped. |
| `patient_verification_rules` | Strong vs weak match policy (below). |
| `success_condition` | The ONLY condition under which success may be reported. |
| `failure_modes` | Known ways it fails. |
| `retry_behavior` | When/whether to retry. |
| `approval_requirements` | Human (and provider, for notes) approval rules. |
| `post_action_verification` | Mandatory re-read + screenshot proof for writes. |
| `output_schema` | Reference to the standard bridge response model. |
| `audit_proof_required` | Always true. |
| `implementation_status` | `IMPLEMENTED_VENDORED` or `BLOCKED_PENDING_REAL_SELECTOR_OR_CREDENTIALS`. |
| `implemented_by` | (implemented actions) which vendored method backs it. |
| `blocked_reason` / `needed_from_user` | (blocked actions) why, and what real input unblocks it. |

## Patient verification (non-negotiable)

- **Strong match** — may proceed: `emr_id`, **or** `dob + exact first_name + last_name`, **or** `dob + phone`.
- **Weak match** — name only / phone only / email only: **HALT**. Enqueue the
  `human_review_queue` and return `needs_human_review`. Never act on a weak match.
- **No match**: return `failed` with `match_level = none`. Never guess.

## Implementation status (current)

`IMPLEMENTED_VENDORED` only where a module in `bridge/integrations/` genuinely
implements the action (verified against the actual code):

- **Implemented (8):** `find_patient`, `get_patient_demographics`,
  `get_upcoming_appointments`, `retrieve_notes`, `book_appointment`,
  `cancel_appointment`, `create_new_patient` (dry-run discovery implemented;
  the commit/Save path is gated and returns `save_unverified` until the Save
  POST contract is HAR-verified), `retrieve_note`.
- **Blocked (16):** everything else — no verified selector or wire contract
  exists yet in the vendored SIS/Svigg clients. These return a structured
  `blocked` response at runtime; they never fabricate a result.

## Adding or promoting a contract

1. **Map the real EMR interaction first.** Capture a HAR of a human performing
   the operation (see `SVIGG_SCHEDULING_CONTRACT.md` for the pattern). Never
   invent a selector or endpoint.
2. Implement it in the relevant vendored client under `bridge/integrations/`,
   plus a wrapper in `bridge/adapters/`.
3. Add/adjust the entry in `actions.json`: fill every contract field, set
   `implemented_by`, and flip `implementation_status` to `IMPLEMENTED_VENDORED`.
   For writes, `post_action_verification.mandatory` must stay `true`.
4. Add the `action_name` to the enum in `action_intent.schema.json`.
5. Add a POST endpoint in the matching `bridge/routers/*` and wire the adapter.
6. Load `actions.json` into the `action_registry` table (the DB projection).
7. Test writes against the allowlisted **test patient only** until trusted.

Never flip a status to `IMPLEMENTED_VENDORED` on faith — it must be backed by
code that actually drives the real EMR and verifies the result.

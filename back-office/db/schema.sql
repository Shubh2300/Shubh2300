-- =====================================================================
-- Back Office — Postgres 16 schema
-- Surgical-center workflow platform ("Verified EMR Action Bridge")
--
-- Design principles baked into this schema:
--   * Every table has created_at + updated_at (updated_at maintained by a
--     shared trigger; see set_updated_at()).
--   * NO PHI-at-rest guarantees are made HERE beyond structure — the
--     application/bridge layer decides what identifiers land in which
--     column. audit_logs deliberately stores only the *identifiers used to
--     match* (not full charts) and is hash-chained + append-only.
--   * audit_logs is APPEND-ONLY and TAMPER-EVIDENT: UPDATE/DELETE are blocked
--     by triggers, and every row carries prev_hash + entry_hash forming a
--     SHA-256 hash chain (mirrors n8n-office/python/integrations/audit_log.py).
--   * The action_registry table is the DB projection of
--     packages/action-registry/actions.json — one row per approved action.
--
-- Requires: Postgres 16 (uses gen_random_uuid() from core, JSONB, ENUM types).
-- =====================================================================

BEGIN;

-- gen_random_uuid() is in core since PG13, but pgcrypto also provides digest()
-- used by the audit hash-chain trigger below.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- Shared enums
-- ---------------------------------------------------------------------
CREATE TYPE target_system     AS ENUM ('sis', 'svigg', 'both', 'unknown');
CREATE TYPE risk_level        AS ENUM ('1', '2', '3', '4');           -- read / schedule / patient / notes
CREATE TYPE run_status        AS ENUM ('pending', 'planning', 'awaiting_approval',
                                       'approved', 'executing', 'verifying',
                                       'success', 'failed', 'blocked',
                                       'needs_human_review', 'cancelled');
CREATE TYPE step_status       AS ENUM ('pending', 'running', 'success', 'failed', 'skipped', 'blocked');
CREATE TYPE approval_status   AS ENUM ('pending', 'approved', 'rejected', 'expired', 'cancelled');
CREATE TYPE match_level       AS ENUM ('strong', 'weak', 'none');
CREATE TYPE task_status       AS ENUM ('open', 'in_progress', 'blocked', 'done', 'cancelled');
CREATE TYPE review_status     AS ENUM ('queued', 'claimed', 'resolved', 'dismissed');
CREATE TYPE impl_status       AS ENUM ('IMPLEMENTED_VENDORED',
                                       'BLOCKED_PENDING_REAL_SELECTOR_OR_CREDENTIALS');

-- ---------------------------------------------------------------------
-- Shared updated_at trigger
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- Tenancy / identity
-- =====================================================================
CREATE TABLE organizations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    slug          TEXT NOT NULL UNIQUE,
    timezone      TEXT NOT NULL DEFAULT 'America/New_York',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_organizations_updated BEFORE UPDATE ON organizations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE roles (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,               -- e.g. 'front_desk', 'biller', 'provider', 'admin'
    description    TEXT,
    -- v1: any staff member can approve anything (DECISIONS.md #11). This bitmask
    -- of permitted risk levels is stored now, enforced later.
    can_approve_risk_levels risk_level[] NOT NULL DEFAULT ARRAY[]::risk_level[],
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, name)
);
CREATE TRIGGER trg_roles_updated BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role_id         UUID REFERENCES roles(id) ON DELETE SET NULL,
    email           TEXT NOT NULL,
    full_name       TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, email)
);
CREATE INDEX idx_users_org ON users(organization_id);
CREATE TRIGGER trg_users_updated BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- EMR systems + credential *references* (never the secrets themselves)
-- =====================================================================
CREATE TABLE emr_systems (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    system_key      target_system NOT NULL,           -- 'sis' | 'svigg'
    display_name    TEXT NOT NULL,
    base_url        TEXT,                              -- non-secret
    access_pattern  TEXT NOT NULL,                     -- 'rest_via_playwright' | 'browser_rpa'
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, system_key)
);
CREATE TRIGGER trg_emr_systems_updated BEFORE UPDATE ON emr_systems
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Only the NAMES of env vars / secret-store keys live here. NO passwords,
-- tokens, or session cookies are ever stored in Postgres (DECISIONS.md #3).
CREATE TABLE emr_credentials_reference (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    emr_system_id   UUID NOT NULL REFERENCES emr_systems(id) ON DELETE CASCADE,
    credential_role TEXT NOT NULL,                     -- 'username' | 'password' | 'url'
    env_var_name    TEXT NOT NULL,                     -- e.g. 'SIS_USERNAME', 'WEBEDOCTOR_PASS'
    secret_store_ref TEXT,                             -- optional pointer into a vault
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (emr_system_id, credential_role)
);
CREATE TRIGGER trg_emr_creds_updated BEFORE UPDATE ON emr_credentials_reference
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Patients + external ids (the platform builds its OWN patient index —
-- DECISIONS.md #4 — cross-referenced to each EMR by external id)
-- =====================================================================
CREATE TABLE patients (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    -- Minimal demographic index. Kept intentionally small; full chart lives in
    -- the EMRs, not here.
    first_name      TEXT,
    last_name       TEXT,
    dob             DATE,
    phone           TEXT,
    email           TEXT,
    is_test_patient BOOLEAN NOT NULL DEFAULT FALSE,    -- Shubh's allowlisted test record
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_patients_org ON patients(organization_id);
CREATE INDEX idx_patients_name ON patients(organization_id, lower(last_name), lower(first_name));
CREATE INDEX idx_patients_dob ON patients(organization_id, dob);
CREATE TRIGGER trg_patients_updated BEFORE UPDATE ON patients
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- One row per (patient, EMR) — e.g. SIS numeric patient_id, Svigg acct/rowid.
CREATE TABLE patient_external_ids (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id      UUID NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    emr_system_id   UUID NOT NULL REFERENCES emr_systems(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,                     -- SIS patient_id / Svigg acct
    external_id_kind TEXT NOT NULL DEFAULT 'primary',  -- 'primary' | 'acct' | 'rowid'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (emr_system_id, external_id, external_id_kind)
);
CREATE INDEX idx_patient_ext_patient ON patient_external_ids(patient_id);
CREATE TRIGGER trg_patient_ext_updated BEFORE UPDATE ON patient_external_ids
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Action registry (DB projection of packages/action-registry/actions.json)
-- =====================================================================
CREATE TABLE action_registry (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_name            TEXT NOT NULL UNIQUE,       -- e.g. 'book_appointment'
    target_system          target_system NOT NULL,
    risk_level             risk_level NOT NULL,
    requires_approval      BOOLEAN NOT NULL,
    required_inputs        JSONB NOT NULL DEFAULT '[]'::jsonb,
    preconditions          JSONB NOT NULL DEFAULT '[]'::jsonb,
    selectors_needed       JSONB NOT NULL DEFAULT '[]'::jsonb,
    patient_verification_rules JSONB NOT NULL DEFAULT '{}'::jsonb,
    success_condition      TEXT,
    failure_modes          JSONB NOT NULL DEFAULT '[]'::jsonb,
    retry_behavior         TEXT,
    post_action_verification JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_schema_ref      TEXT,
    audit_proof_required   BOOLEAN NOT NULL DEFAULT TRUE,
    implementation_status  impl_status NOT NULL,
    contract              JSONB NOT NULL DEFAULT '{}'::jsonb,  -- full actions.json entry
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_action_registry_impl ON action_registry(implementation_status);
CREATE TRIGGER trg_action_registry_updated BEFORE UPDATE ON action_registry
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Tasks (staff-facing work items)
-- =====================================================================
CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    patient_id      UUID REFERENCES patients(id) ON DELETE SET NULL,
    created_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    assigned_to     UUID REFERENCES users(id) ON DELETE SET NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    status          task_status NOT NULL DEFAULT 'open',
    due_at          TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_tasks_org_status ON tasks(organization_id, status);
CREATE INDEX idx_tasks_patient ON tasks(patient_id);
CREATE TRIGGER trg_tasks_updated BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Workflow runs (Temporal-backed orchestration) and their steps
-- =====================================================================
CREATE TABLE workflow_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    workflow_type     TEXT NOT NULL,                   -- e.g. 'referral_intake', 'reschedule'
    temporal_workflow_id TEXT,                          -- Temporal correlation id
    temporal_run_id   TEXT,
    initiated_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    patient_id        UUID REFERENCES patients(id) ON DELETE SET NULL,
    status            run_status NOT NULL DEFAULT 'pending',
    input             JSONB NOT NULL DEFAULT '{}'::jsonb,
    result            JSONB,
    failure_reason    TEXT,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_workflow_runs_org_status ON workflow_runs(organization_id, status);
CREATE TRIGGER trg_workflow_runs_updated BEFORE UPDATE ON workflow_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE workflow_steps (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_run_id   UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,
    name              TEXT NOT NULL,
    status            step_status NOT NULL DEFAULT 'pending',
    input             JSONB,
    output            JSONB,
    failure_reason    TEXT,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workflow_run_id, seq)
);
CREATE INDEX idx_workflow_steps_run ON workflow_steps(workflow_run_id);
CREATE TRIGGER trg_workflow_steps_updated BEFORE UPDATE ON workflow_steps
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Evidence: screenshots + Playwright traces (files live on disk in the
-- bridge; these rows are the metadata + integrity pointer)
-- =====================================================================
CREATE TABLE screenshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    screenshot_key  TEXT NOT NULL UNIQUE,              -- bridge-generated id (evidence.py)
    emr_system      target_system,
    file_path       TEXT NOT NULL,                     -- path on the office machine
    sha256          TEXT,                              -- integrity of the image bytes
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_screenshots_updated BEFORE UPDATE ON screenshots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE traces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES organizations(id) ON DELETE SET NULL,
    trace_key       TEXT NOT NULL UNIQUE,              -- bridge-generated id (evidence.py)
    emr_system      target_system,
    file_path       TEXT NOT NULL,                     -- Playwright trace.zip path
    sha256          TEXT,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_traces_updated BEFORE UPDATE ON traces
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Action runs (one per Action-Registry action invocation) and their steps
-- =====================================================================
CREATE TABLE action_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    action_registry_id UUID NOT NULL REFERENCES action_registry(id) ON DELETE RESTRICT,
    workflow_run_id   UUID REFERENCES workflow_runs(id) ON DELETE SET NULL,
    task_id           UUID REFERENCES tasks(id) ON DELETE SET NULL,
    patient_id        UUID REFERENCES patients(id) ON DELETE SET NULL,
    requested_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    target_system     target_system NOT NULL,
    risk_level        risk_level NOT NULL,
    status            run_status NOT NULL DEFAULT 'pending',
    -- The staff request, parsed intent, and the identifiers actually used to
    -- resolve the patient (mirrors what the bridge received).
    staff_prompt      TEXT,
    parsed_intent     JSONB,
    patient_identifiers_used JSONB,
    action_inputs     JSONB,
    match_level       match_level,
    matched_by        TEXT[],
    verified          BOOLEAN,
    pre_action_state  JSONB,
    post_action_state JSONB,
    result            JSONB,
    failure_reason    TEXT,
    screenshot_id     UUID REFERENCES screenshots(id) ON DELETE SET NULL,
    trace_id          UUID REFERENCES traces(id) ON DELETE SET NULL,
    requires_human_review BOOLEAN NOT NULL DEFAULT FALSE,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_action_runs_org_status ON action_runs(organization_id, status);
CREATE INDEX idx_action_runs_patient ON action_runs(patient_id);
CREATE INDEX idx_action_runs_action ON action_runs(action_registry_id);
CREATE TRIGGER trg_action_runs_updated BEFORE UPDATE ON action_runs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE action_steps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_run_id   UUID NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    name            TEXT NOT NULL,                      -- 'resolve_patient', 'fill_form', 'commit', 'verify'
    status          step_status NOT NULL DEFAULT 'pending',
    request_payload JSONB,
    response_payload JSONB,
    screenshot_id   UUID REFERENCES screenshots(id) ON DELETE SET NULL,
    failure_reason  TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (action_run_id, seq)
);
CREATE INDEX idx_action_steps_run ON action_steps(action_run_id);
CREATE TRIGGER trg_action_steps_updated BEFORE UPDATE ON action_steps
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Approvals (two-phase: plan -> human approval -> commit)
-- =====================================================================
CREATE TABLE approvals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    action_run_id   UUID NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
    requested_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    approver_id     UUID REFERENCES users(id) ON DELETE SET NULL,
    status          approval_status NOT NULL DEFAULT 'pending',
    risk_level      risk_level NOT NULL,
    -- Opaque single-use token the bridge checks before a write executes. Store
    -- only a hash of the token, never the token itself.
    approval_token_hash TEXT,
    proposed_action JSONB NOT NULL,                     -- the plan the human sees
    decision_reason TEXT,
    decided_at      TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_approvals_org_status ON approvals(organization_id, status);
CREATE INDEX idx_approvals_action_run ON approvals(action_run_id);
CREATE TRIGGER trg_approvals_updated BEFORE UPDATE ON approvals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- Domain-specific action tables
-- =====================================================================
CREATE TABLE appointment_actions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_run_id   UUID NOT NULL REFERENCES action_runs(id) ON DELETE CASCADE,
    patient_id      UUID REFERENCES patients(id) ON DELETE SET NULL,
    emr_system      target_system NOT NULL,
    operation       TEXT NOT NULL,                      -- 'book' | 'cancel' | 'reschedule' | 'confirm' | 'no_show' | 'note'
    encounter_id    TEXT,                               -- Svigg enc / SIS case id
    appointment_date DATE,
    appointment_time TEXT,
    provider        TEXT,
    office          TEXT,
    cpt_code        TEXT,
    duration_minutes INTEGER,
    note            TEXT,
    verified        BOOLEAN,
    verification_detail JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_appt_actions_run ON appointment_actions(action_run_id);
CREATE INDEX idx_appt_actions_patient ON appointment_actions(patient_id);
CREATE TRIGGER trg_appt_actions_updated BEFORE UPDATE ON appointment_actions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE patient_creation_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_run_id   UUID REFERENCES action_runs(id) ON DELETE SET NULL,
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    emr_system      target_system NOT NULL,
    -- Proposed demographics for the NEW chart. Kept here so a human can review
    -- before commit; the commit path in the scraper is dry-run by default.
    demographics    JSONB NOT NULL,
    dedupe_result   JSONB,                              -- suspected-duplicate matches from the EMR
    dry_run         BOOLEAN NOT NULL DEFAULT TRUE,
    committed       BOOLEAN NOT NULL DEFAULT FALSE,
    resulting_external_id TEXT,                          -- populated only on a verified create
    verified        BOOLEAN,
    status          run_status NOT NULL DEFAULT 'pending',
    failure_reason  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_patient_creation_org ON patient_creation_requests(organization_id);
CREATE TRIGGER trg_patient_creation_updated BEFORE UPDATE ON patient_creation_requests
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Immutable snapshots of a clinical note before/after an action (notes are
-- addendum-only; unsigned notes may be updated, signed notes NEVER edited).
CREATE TABLE note_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_run_id   UUID REFERENCES action_runs(id) ON DELETE SET NULL,
    patient_id      UUID REFERENCES patients(id) ON DELETE SET NULL,
    emr_system      target_system NOT NULL,
    emr_note_id     TEXT,
    note_category   TEXT,
    is_signed       BOOLEAN,
    snapshot_kind   TEXT NOT NULL,                      -- 'pre' | 'post' | 'addendum'
    content_hash    TEXT NOT NULL,                      -- SHA-256 of captured content
    content_ref     TEXT,                               -- pointer to secured storage (never inline PHI)
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_note_snapshots_run ON note_snapshots(action_run_id);
CREATE INDEX idx_note_snapshots_patient ON note_snapshots(patient_id);
CREATE TRIGGER trg_note_snapshots_updated BEFORE UPDATE ON note_snapshots
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Weak-match / ambiguous / unverified events that MUST stop for a human.
CREATE TABLE human_review_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    action_run_id   UUID REFERENCES action_runs(id) ON DELETE SET NULL,
    reason_code     TEXT NOT NULL,                      -- 'weak_match' | 'ambiguous_name' | 'unverified_write' | 'blocked'
    reason_detail   TEXT,
    payload         JSONB,                              -- candidates, evidence, etc.
    status          review_status NOT NULL DEFAULT 'queued',
    claimed_by      UUID REFERENCES users(id) ON DELETE SET NULL,
    resolved_by     UUID REFERENCES users(id) ON DELETE SET NULL,
    resolution      TEXT,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_human_review_org_status ON human_review_queue(organization_id, status);
CREATE TRIGGER trg_human_review_updated BEFORE UPDATE ON human_review_queue
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- AUDIT LOG — append-only, hash-chained, tamper-evident
-- Mirrors n8n-office/python/integrations/audit_log.py:
--   * INSERT only (UPDATE/DELETE blocked by triggers below)
--   * prev_hash + entry_hash form a SHA-256 chain; any silent edit/delete of an
--     earlier row breaks verification from that point onward.
-- Note: entry_hash is computed by a BEFORE INSERT trigger so the application
-- cannot forge or forget it.
-- =====================================================================
CREATE TABLE audit_logs (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id       UUID REFERENCES organizations(id) ON DELETE SET NULL,
    action_run_id         UUID,                          -- soft ref (no FK: audit must survive row deletes elsewhere)
    -- WHO / WHY / WHAT
    actor_user_id         UUID,                          -- soft ref to users(id)
    actor_label           TEXT NOT NULL,                 -- username/email/system that acted
    staff_prompt          TEXT,                          -- the raw staff request
    parsed_intent         JSONB,                         -- parsed intent JSON
    approver_label        TEXT,                          -- who approved (if a write)
    target_system         target_system NOT NULL,
    action                TEXT NOT NULL,                 -- action_name from the registry
    -- Patient linkage: identifiers USED to match (not the full chart). Free to
    -- be hashed/tokenized by the application before insert.
    patient_identifiers_used JSONB,
    match_result          match_level,
    matched_by            TEXT[],
    -- STATE
    pre_action_state      JSONB,
    post_action_state     JSONB,
    -- EVIDENCE
    screenshot_id         UUID,                          -- soft ref to screenshots(id)
    trace_id              UUID,                          -- soft ref to traces(id)
    -- OUTCOME
    result                TEXT NOT NULL,                 -- 'success'|'failed'|'blocked'|'needs_human_review'
    failure_reason        TEXT,
    -- HASH CHAIN
    prev_hash             CHAR(64) NOT NULL,
    entry_hash            CHAR(64) NOT NULL UNIQUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
    -- NOTE: no updated_at — this table is append-only and never mutated.
);
CREATE INDEX idx_audit_logs_org ON audit_logs(organization_id);
CREATE INDEX idx_audit_logs_action_run ON audit_logs(action_run_id);
CREATE INDEX idx_audit_logs_actor ON audit_logs(actor_label);
CREATE INDEX idx_audit_logs_created ON audit_logs(created_at);

-- Genesis prev_hash for the first row (mirror of GENESIS_PREV_HASH = "0"*64).
-- The chain hashes a canonical field set + prev_hash. Field order is
-- load-bearing: changing it invalidates every existing chain.
CREATE OR REPLACE FUNCTION audit_logs_hash_chain() RETURNS trigger AS $$
DECLARE
    v_prev   CHAR(64);
    v_canon  TEXT;
BEGIN
    -- Fetch the most recent entry_hash as this row's prev_hash. A single
    -- serialized writer is assumed (bridge writes audit rows one at a time);
    -- for stronger concurrency guarantees, wrap inserts in SERIALIZABLE.
    SELECT entry_hash INTO v_prev
      FROM audit_logs
      ORDER BY id DESC
      LIMIT 1;

    IF v_prev IS NULL THEN
        v_prev := repeat('0', 64);
    END IF;

    NEW.prev_hash := v_prev;

    -- Canonical serialization of the hashed fields + prev_hash. Uses a fixed
    -- key order via a JSONB build; digest() from pgcrypto gives SHA-256.
    v_canon := coalesce(NEW.actor_label,'')          || '|' ||
               coalesce(NEW.staff_prompt,'')          || '|' ||
               coalesce(NEW.parsed_intent::text,'')   || '|' ||
               coalesce(NEW.approver_label,'')        || '|' ||
               coalesce(NEW.target_system::text,'')   || '|' ||
               coalesce(NEW.action,'')                || '|' ||
               coalesce(NEW.patient_identifiers_used::text,'') || '|' ||
               coalesce(NEW.match_result::text,'')    || '|' ||
               coalesce(array_to_string(NEW.matched_by, ','),'') || '|' ||
               coalesce(NEW.pre_action_state::text,'')  || '|' ||
               coalesce(NEW.post_action_state::text,'') || '|' ||
               coalesce(NEW.screenshot_id::text,'')   || '|' ||
               coalesce(NEW.trace_id::text,'')        || '|' ||
               coalesce(NEW.result,'')                || '|' ||
               coalesce(NEW.failure_reason,'')        || '|' ||
               coalesce(NEW.created_at::text, now()::text) || '|' ||
               v_prev;

    NEW.entry_hash := encode(digest(v_canon, 'sha256'), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_logs_hash_chain
    BEFORE INSERT ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_hash_chain();

-- Block UPDATE and DELETE at the DB layer (belt-and-braces, like the SQLite
-- triggers in audit_log.py). The application also never issues them.
CREATE OR REPLACE FUNCTION audit_logs_block_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_logs_no_update
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_block_mutation();

CREATE TRIGGER trg_audit_logs_no_delete
    BEFORE DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_block_mutation();

COMMIT;

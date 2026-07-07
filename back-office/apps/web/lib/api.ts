/**
 * Typed API client for the Back Office backend (apps/api, FastAPI).
 *
 * Single source of truth for NEXT_PUBLIC_API_URL and every fetch call the
 * dashboard makes. No page should call `fetch` directly.
 *
 * IMPORTANT — backend shape assumptions:
 * The backend was being built concurrently by another agent. At the time this
 * client was written, `apps/api/schemas.py` and `db/schema.sql` existed but
 * the HTTP routers did not, and the two sources disagree on some field names
 * (e.g. schemas.py's ApprovalOut has a `state` field using a merged
 * proposed/approved/executing/verified/failed enum, while db/schema.sql gives
 * `approvals.status` a narrower pending/approved/rejected/expired/cancelled
 * enum and puts the richer run lifecycle on `action_runs.status` instead —
 * which matches what this app was asked to build against: run status of
 * success | failed | blocked | needs_human_review).
 *
 * To stay usable regardless of which naming the routers ship with, every
 * `normalize*` function below accepts the raw JSON as `any` and reads several
 * plausible field names, falling back to `undefined`/`'—'` rather than ever
 * inventing a value. The original payload is preserved on `.raw` in case a
 * page needs a field this client doesn't know about yet.
 */

export const API_BASE = (process.env.NEXT_PUBLIC_API_URL ?? '').replace(/\/+$/, '');

export class ApiError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  if (!API_BASE) {
    throw new ApiError(
      'NEXT_PUBLIC_API_URL is not configured. Set it in .env.local and restart the dev server.'
    );
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      cache: 'no-store',
      ...init,
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    // Network failure, DNS failure, CORS failure, server not running, etc.
    throw new ApiError('API unreachable');
  }

  if (!res.ok) {
    let detail = '';
    try {
      detail = await res.text();
    } catch {
      /* ignore */
    }
    throw new ApiError(
      `Request failed (${res.status} ${res.statusText})${detail ? `: ${detail}` : ''}`,
      res.status
    );
  }

  if (res.status === 204) {
    return undefined as T;
  }

  try {
    return (await res.json()) as T;
  } catch {
    return undefined as T;
  }
}

// --------------------------------------------------------------------------
// Shared value types
// --------------------------------------------------------------------------

export interface PatientRef {
  patientId?: string;
  firstName?: string;
  lastName?: string;
  dob?: string;
  systemIds?: Record<string, string>;
}

/** risk_level enum from db/schema.sql: '1' read, '2' schedule, '3' patient data, '4' notes. */
export const RISK_LEVEL_LABELS: Record<string, string> = {
  '1': 'read-only',
  '2': 'schedule change',
  '3': 'patient data change',
  '4': 'clinical note',
};

export function formatRiskLevel(riskLevel?: string): string {
  if (!riskLevel) return '—';
  const label = RISK_LEVEL_LABELS[riskLevel];
  return label ? `${riskLevel} — ${label}` : riskLevel;
}

function normalizePatientRef(raw: any): PatientRef | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const ref: PatientRef = {
    patientId: raw.patient_id ?? raw.patientId ?? raw.id ?? undefined,
    firstName: raw.first_name ?? raw.firstName ?? undefined,
    lastName: raw.last_name ?? raw.lastName ?? undefined,
    dob: raw.dob ?? undefined,
    systemIds: raw.system_ids ?? raw.systemIds ?? undefined,
  };
  const hasAny = Object.values(ref).some((v) => v !== undefined);
  return hasAny ? ref : undefined;
}

export function patientDisplayName(p?: PatientRef): string {
  if (!p) return '—';
  const name = [p.firstName, p.lastName].filter(Boolean).join(' ').trim();
  return name || p.patientId || '—';
}

// --------------------------------------------------------------------------
// Tasks
// --------------------------------------------------------------------------

export interface Task {
  id: string;
  title: string;
  description?: string;
  status?: string;
  createdBy?: string;
  createdAt?: string;
  updatedAt?: string;
  raw: unknown;
}

function normalizeTask(raw: any): Task {
  return {
    id: String(raw.id),
    title: raw.title ?? raw.prompt ?? raw.description ?? '(no description)',
    description: raw.description ?? raw.prompt ?? undefined,
    status: raw.status ?? undefined,
    createdBy: raw.created_by ?? raw.createdBy ?? undefined,
    createdAt: raw.created_at ?? raw.createdAt ?? undefined,
    updatedAt: raw.updated_at ?? raw.updatedAt ?? undefined,
    raw,
  };
}

export async function listTasks(): Promise<Task[]> {
  const rows = await apiFetch<any[]>('/tasks');
  return (rows ?? []).map(normalizeTask);
}

export async function createTask(input: {
  prompt: string;
  createdBy: string;
}): Promise<Task> {
  const row = await apiFetch<any>('/tasks', {
    method: 'POST',
    body: JSON.stringify({ prompt: input.prompt, created_by: input.createdBy }),
  });
  return normalizeTask(row);
}

// --------------------------------------------------------------------------
// Intent parsing
// --------------------------------------------------------------------------

export interface ActionIntent {
  action: string;
  system?: string;
  riskLevel?: string;
  patient?: PatientRef;
  inputs?: Record<string, unknown>;
  missingFields?: string[];
  reason?: string;
  confidence?: number;
  raw: unknown;
}

export interface IntentValidation {
  ok: boolean;
  status: string;
  unknownAction: boolean;
  missingInputs: string[];
  errors: string[];
}

export interface ParsedIntentResult {
  intent: ActionIntent;
  validation?: IntentValidation;
}

function normalizeIntent(raw: any): ActionIntent {
  // Real backend shape (action_intent.schema.json): action_name, target_system,
  // risk_level, action_inputs, patient_identifiers, missing_fields, reason.
  return {
    action: raw.action_name ?? raw.action ?? '—',
    system: raw.target_system ?? raw.system ?? undefined,
    riskLevel: raw.risk_level != null ? String(raw.risk_level) : undefined,
    patient: normalizePatientRef(raw.patient_identifiers ?? raw.patient),
    inputs: raw.action_inputs ?? raw.inputs ?? undefined,
    missingFields: raw.missing_fields ?? raw.missingFields ?? undefined,
    reason: raw.reason ?? raw.rationale ?? undefined,
    confidence: raw.confidence ?? undefined,
    raw,
  };
}

export async function parseIntent(input: {
  prompt: string;
  requestedBy?: string;
  taskId?: string;
}): Promise<ParsedIntentResult> {
  const payload = await apiFetch<any>('/intents/parse', {
    method: 'POST',
    body: JSON.stringify({
      prompt: input.prompt,
      requested_by: input.requestedBy || undefined,
      task_id: input.taskId || undefined,
    }),
  });

  // Tolerate either { intent, validation } or a bare intent object.
  const rawIntent = payload?.intent ?? payload;
  const rawValidation = payload?.validation;

  return {
    intent: normalizeIntent(rawIntent),
    validation: rawValidation
      ? {
          ok: !!rawValidation.ok,
          status: rawValidation.status ?? (rawValidation.ok ? 'accepted' : 'rejected'),
          unknownAction: !!rawValidation.unknown_action,
          missingInputs: rawValidation.missing_inputs ?? [],
          errors: rawValidation.errors ?? [],
        }
      : undefined,
  };
}

// --------------------------------------------------------------------------
// Approvals
// --------------------------------------------------------------------------

export interface Approval {
  id: string;
  action: string;
  system?: string;
  riskLevel?: string;
  status: string;
  inputs?: Record<string, unknown>;
  patient?: PatientRef;
  requestedBy?: string;
  approvedBy?: string;
  reason?: string;
  createdAt?: string;
  decidedAt?: string;
  raw: unknown;
}

function normalizeApproval(raw: any): Approval {
  // Real backend: ApprovalOut carries action_name + status (approval_status:
  // pending/approved/rejected/expired/cancelled) + proposed_action (the
  // reviewed ActionIntent, which holds target_system / action_inputs /
  // patient_identifiers).
  const intent = raw.proposed_action ?? raw.intent ?? {};
  return {
    id: String(raw.id),
    action: raw.action_name ?? intent.action_name ?? '—',
    system: intent.target_system ?? raw.target_system ?? undefined,
    riskLevel: raw.risk_level != null ? String(raw.risk_level) : undefined,
    status: raw.status ?? 'pending',
    inputs: intent.action_inputs ?? undefined,
    patient: normalizePatientRef(intent.patient_identifiers),
    requestedBy: raw.requested_by ?? undefined,
    approvedBy: raw.approver_id ?? raw.approver_label ?? undefined,
    reason: raw.decision_reason ?? undefined,
    createdAt: raw.created_at ?? undefined,
    decidedAt: raw.decided_at ?? undefined,
    raw,
  };
}

export async function listApprovals(): Promise<Approval[]> {
  const rows = await apiFetch<any[]>('/approvals');
  return (rows ?? []).map(normalizeApproval);
}

/**
 * Submits a parsed, reviewed ActionIntent into the approval queue.
 * Endpoint inferred from apps/api/schemas.py's SubmitForApprovalRequest
 * (intent, task_id, submitted_by) — no other endpoint in the codebase
 * accepts an ActionIntent payload.
 */
export async function submitForApproval(input: {
  intent: ActionIntent;
  taskId?: string;
  submittedBy: string;
  staffPrompt?: string;
}): Promise<Approval> {
  const row = await apiFetch<any>('/approvals', {
    method: 'POST',
    body: JSON.stringify({
      // The API's SubmitForApprovalRequest expects the strict ActionIntent
      // (action_name/target_system/…) — send the raw backend intent.
      intent: input.intent.raw ?? input.intent,
      task_id: input.taskId || undefined,
      requested_by: input.submittedBy,
      staff_prompt: input.staffPrompt || undefined,
    }),
  });
  return normalizeApproval(row);
}

export async function approveApproval(id: string, actingUser: string): Promise<Approval> {
  const row = await apiFetch<any>(`/approvals/${id}/approve`, {
    method: 'POST',
    headers: { 'X-Acting-User': actingUser },
    body: JSON.stringify({ approver_user_id: actingUser }),
  });
  return normalizeApproval(row);
}

export async function rejectApproval(
  id: string,
  actingUser: string,
  reason?: string
): Promise<Approval> {
  const row = await apiFetch<any>(`/approvals/${id}/reject`, {
    method: 'POST',
    headers: { 'X-Acting-User': actingUser },
    body: JSON.stringify({ approver_user_id: actingUser, reason: reason || undefined }),
  });
  return normalizeApproval(row);
}

// --------------------------------------------------------------------------
// Action runs
// --------------------------------------------------------------------------

export interface ActionRunHistoryEntry {
  status: string;
  at?: string;
  note?: string;
}

export interface ActionRun {
  id: string;
  action?: string;
  system?: string;
  /** Bridge/execution outcome: success | failed | blocked | needs_human_review | executing | ... */
  status: string;
  verified?: boolean;
  screenshotId?: string;
  traceId?: string;
  failureReason?: string;
  warnings?: string[];
  createdAt?: string;
  updatedAt?: string;
  history?: ActionRunHistoryEntry[];
  raw: unknown;
}

function normalizeActionRun(raw: any): ActionRun {
  // Real backend: ActionRunOut. status is the run_status enum
  // (pending/planning/awaiting_approval/approved/executing/verifying/success/
  // failed/blocked/needs_human_review/cancelled). screenshot_id / trace_id are
  // the bridge-generated evidence KEY strings (resolved from the FK rows).
  // action_runs has no warnings column — warnings live in the result JSONB.
  const result = raw.result ?? {};
  return {
    id: String(raw.id),
    action: raw.action_name ?? raw.action ?? undefined,
    system: raw.target_system ?? raw.system ?? undefined,
    status: raw.status ?? 'unknown',
    verified: raw.verified ?? undefined,
    screenshotId: raw.screenshot_id ?? undefined,
    traceId: raw.trace_id ?? undefined,
    failureReason: raw.failure_reason ?? result.failure_reason ?? undefined,
    warnings: result.warnings ?? undefined,
    createdAt: raw.created_at ?? undefined,
    updatedAt: raw.updated_at ?? undefined,
    history: raw.history ?? undefined,
    raw,
  };
}

export async function listActionRuns(): Promise<ActionRun[]> {
  const rows = await apiFetch<any[]>('/action_runs');
  return (rows ?? []).map(normalizeActionRun);
}

export async function getActionRun(id: string): Promise<ActionRun> {
  const row = await apiFetch<any>(`/action_runs/${id}`);
  return normalizeActionRun(row);
}

/** Evidence (screenshot / Playwright trace) link, per the spec's `${API}/evidence/{id}` convention. */
export function evidenceUrl(id: string): string {
  return `${API_BASE}/evidence/${id}`;
}

// --------------------------------------------------------------------------
// Audit log
// --------------------------------------------------------------------------

export interface AuditEntry {
  id: string;
  timestamp?: string;
  actor?: string;
  action?: string;
  system?: string;
  resultSummary?: string;
  entryHash?: string;
  raw: unknown;
}

export interface AuditFilters {
  action?: string;
  system?: string;
  date?: string;
}

function normalizeAuditEntry(raw: any): AuditEntry {
  // Real backend: AuditEntryOut. Columns actor_label / target_system / result /
  // failure_reason / entry_hash / created_at (hash chain via DB trigger).
  return {
    id: String(raw.id),
    timestamp: raw.created_at ?? raw.ts ?? undefined,
    actor: raw.actor_label ?? raw.actor ?? undefined,
    action: raw.action ?? undefined,
    system: raw.target_system ?? raw.system ?? undefined,
    resultSummary: raw.failure_reason ?? raw.result ?? undefined,
    entryHash: raw.entry_hash ?? undefined,
    raw,
  };
}

export async function listAudit(filters?: AuditFilters): Promise<AuditEntry[]> {
  const params = new URLSearchParams();
  if (filters?.action) params.set('action', filters.action);
  if (filters?.system) params.set('system', filters.system);
  if (filters?.date) params.set('date', filters.date);
  const qs = params.toString();
  const rows = await apiFetch<any[]>(`/audit${qs ? `?${qs}` : ''}`);
  return (rows ?? []).map(normalizeAuditEntry);
}

// --------------------------------------------------------------------------
// Patients
// --------------------------------------------------------------------------

export interface Patient {
  id: string;
  firstName?: string;
  lastName?: string;
  dob?: string;
  phone?: string;
  email?: string;
  systemIds?: Record<string, string>;
  createdAt?: string;
  raw: unknown;
}

function normalizePatient(raw: any): Patient {
  return {
    id: String(raw.id),
    firstName: raw.first_name ?? raw.firstName ?? undefined,
    lastName: raw.last_name ?? raw.lastName ?? undefined,
    dob: raw.dob ?? undefined,
    phone: raw.phone ?? undefined,
    email: raw.email ?? undefined,
    // Real backend exposes per-EMR ids from patient_external_ids as external_ids.
    systemIds: raw.external_ids ?? raw.system_ids ?? undefined,
    createdAt: raw.created_at ?? undefined,
    raw,
  };
}

export function patientFullName(p: Patient): string {
  const name = [p.firstName, p.lastName].filter(Boolean).join(' ').trim();
  return name || '—';
}

export async function listPatients(): Promise<Patient[]> {
  const rows = await apiFetch<any[]>('/patients');
  return (rows ?? []).map(normalizePatient);
}

export async function getPatient(id: string): Promise<Patient> {
  const row = await apiFetch<any>(`/patients/${id}`);
  return normalizePatient(row);
}

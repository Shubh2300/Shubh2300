'use client';

import Link from 'next/link';
import { useAsync } from '@/lib/useAsync';
import { listTasks, listApprovals, listActionRuns, listAudit, Task, Approval, ActionRun, AuditEntry } from '@/lib/api';

const CLOSED_TASK_STATUSES = new Set(['done', 'cancelled', 'closed', 'completed']);
const IN_FLIGHT_RUN_STATUSES = new Set([
  'proposed',
  'approved',
  'executing',
  'pending',
  'planning',
  'awaiting_approval',
  'verifying',
]);

export default function DashboardPage() {
  const tasks = useAsync(listTasks, []);
  const approvals = useAsync(listApprovals, []);
  const runs = useAsync(listActionRuns, []);
  const audit = useAsync(() => listAudit(), []);

  const openTasks = countIf(
    tasks.data,
    (t: Task) => !t.status || !CLOSED_TASK_STATUSES.has(t.status.toLowerCase())
  );
  const pendingApprovals = countIf(
    approvals.data,
    (a: Approval) => a.status.toLowerCase() === 'pending'
  );
  const runsInFlight = countIf(runs.data, (r: ActionRun) =>
    IN_FLIGHT_RUN_STATUSES.has(r.status.toLowerCase())
  );
  const needsReview = countIf(
    runs.data,
    (r: ActionRun) => r.status.toLowerCase() === 'needs_human_review'
  );

  const recent = [...(audit.data ?? [])]
    .sort((a, b) => toTime(b.timestamp) - toTime(a.timestamp))
    .slice(0, 15);

  return (
    <div>
      <h1>Dashboard</h1>

      <div className="stat-grid">
        <StatTile
          label="Open tasks"
          value={openTasks}
          loading={tasks.loading}
          error={tasks.error}
          href="/tasks"
        />
        <StatTile
          label="Pending approvals"
          value={pendingApprovals}
          loading={approvals.loading}
          error={approvals.error}
          href="/approvals"
        />
        <StatTile
          label="Runs in flight"
          value={runsInFlight}
          loading={runs.loading}
          error={runs.error}
          href="/runs"
        />
        <StatTile
          label="Needs human review"
          value={needsReview}
          loading={runs.loading}
          error={runs.error}
          href="/runs"
          accent="violet"
        />
      </div>

      <section className="panel">
        <h2>Recent activity</h2>
        {audit.loading && <p className="hint">Loading…</p>}
        {audit.error && <p className="error-banner">API unreachable — {audit.error}</p>}
        {!audit.loading && !audit.error && recent.length === 0 && (
          <p className="empty-state">No recent activity.</p>
        )}
        {recent.length > 0 && (
          <ul className="activity-list">
            {recent.map((entry: AuditEntry) => (
              <li key={entry.id}>
                <span className="activity-time">{formatTime(entry.timestamp)}</span>
                <span>
                  {entry.action ?? '—'}
                  {entry.resultSummary ? ` — ${entry.resultSummary}` : ''}
                </span>
                <span className="activity-system">{entry.system ?? '—'}</span>
                <span className="activity-actor">{entry.actor ?? '—'}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function countIf<T>(data: T[] | null, predicate: (item: T) => boolean): number | undefined {
  if (!data) return undefined;
  return data.filter(predicate).length;
}

function toTime(v?: string): number {
  if (!v) return 0;
  const t = new Date(v).getTime();
  return Number.isNaN(t) ? 0 : t;
}

function formatTime(v?: string): string {
  if (!v) return '—';
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? v : d.toLocaleString();
}

function StatTile({
  label,
  value,
  loading,
  error,
  href,
  accent,
}: {
  label: string;
  value?: number;
  loading: boolean;
  error: string | null;
  href: string;
  accent?: 'violet';
}) {
  return (
    <Link href={href} className={`stat-tile${accent ? ` stat-tile-${accent}` : ''}`}>
      <div className="stat-label">{label}</div>
      {loading && <div className="stat-value hint">…</div>}
      {!loading && error && <div className="stat-value error-text">—</div>}
      {!loading && !error && <div className="stat-value">{value ?? 0}</div>}
      {!loading && error && <div className="stat-error">API unreachable</div>}
    </Link>
  );
}

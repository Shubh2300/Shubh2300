'use client';

import Link from 'next/link';
import { useAsync } from '@/lib/useAsync';
import { listActionRuns, ActionRun } from '@/lib/api';
import StatusBadge from '@/components/StatusBadge';

export default function RunsPage() {
  const runs = useAsync(listActionRuns, []);

  return (
    <div>
      <h1>Workflow runs</h1>

      {runs.loading && <p className="hint">Loading…</p>}
      {runs.error && <p className="error-banner">API unreachable — {runs.error}</p>}
      {!runs.loading && !runs.error && (runs.data?.length ?? 0) === 0 && (
        <p className="empty-state">No runs yet.</p>
      )}

      {(runs.data?.length ?? 0) > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Action</th>
              <th>System</th>
              <th>Status</th>
              <th>Verified</th>
              <th>Updated</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {runs.data!.map((r: ActionRun) => (
              <tr key={r.id}>
                <td>{r.action ?? '—'}</td>
                <td>{r.system ?? '—'}</td>
                <td>
                  <StatusBadge status={r.status} />
                </td>
                <td>{r.verified === undefined ? '—' : r.verified ? 'Yes' : 'No'}</td>
                <td>{r.updatedAt ? new Date(r.updatedAt).toLocaleString() : '—'}</td>
                <td>
                  <Link href={`/runs/${r.id}`}>Details →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

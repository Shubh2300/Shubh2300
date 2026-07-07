'use client';

import { useState } from 'react';
import { useAsync } from '@/lib/useAsync';
import { listAudit, AuditFilters, AuditEntry } from '@/lib/api';

export default function AuditPage() {
  const [filters, setFilters] = useState<AuditFilters>({});
  const [draft, setDraft] = useState<AuditFilters>({});
  const audit = useAsync(() => listAudit(filters), [filters.action, filters.system, filters.date]);

  function applyFilters(e: React.FormEvent) {
    e.preventDefault();
    setFilters(draft);
  }

  function clearFilters() {
    setDraft({});
    setFilters({});
  }

  const hasFilters = !!(filters.action || filters.system || filters.date);

  return (
    <div>
      <h1>Audit log</h1>

      <form className="filter-bar" onSubmit={applyFilters}>
        <input
          placeholder="Action"
          value={draft.action ?? ''}
          onChange={(e) => setDraft((p) => ({ ...p, action: e.target.value || undefined }))}
        />
        <input
          placeholder="System"
          value={draft.system ?? ''}
          onChange={(e) => setDraft((p) => ({ ...p, system: e.target.value || undefined }))}
        />
        <input
          type="date"
          value={draft.date ?? ''}
          onChange={(e) => setDraft((p) => ({ ...p, date: e.target.value || undefined }))}
        />
        <button type="submit">Apply</button>
        <button type="button" className="btn-secondary" onClick={clearFilters}>
          Clear
        </button>
      </form>

      {audit.loading && <p className="hint">Loading…</p>}
      {audit.error && <p className="error-banner">API unreachable — {audit.error}</p>}
      {!audit.loading && !audit.error && (audit.data?.length ?? 0) === 0 && (
        <p className="empty-state">
          {hasFilters ? 'No audit entries match these filters.' : 'No audit entries.'}
        </p>
      )}

      {(audit.data?.length ?? 0) > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Timestamp</th>
              <th>Action</th>
              <th>System</th>
              <th>Actor</th>
              <th>Result</th>
              <th>Hash</th>
            </tr>
          </thead>
          <tbody>
            {audit.data!.map((e: AuditEntry) => (
              <tr key={e.id}>
                <td>{formatTime(e.timestamp)}</td>
                <td>{e.action ?? '—'}</td>
                <td>{e.system ?? '—'}</td>
                <td>{e.actor ?? '—'}</td>
                <td>{e.resultSummary ?? '—'}</td>
                <td className="mono" title={e.entryHash}>
                  {e.entryHash ? `${e.entryHash.slice(0, 10)}…` : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function formatTime(v?: string): string {
  if (!v) return '—';
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? v : d.toLocaleString();
}

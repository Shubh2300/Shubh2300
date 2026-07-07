'use client';

import { useState } from 'react';
import { useAsync } from '@/lib/useAsync';
import {
  listApprovals,
  approveApproval,
  rejectApproval,
  formatRiskLevel,
  patientDisplayName,
  Approval,
} from '@/lib/api';
import { getActingUser } from '@/lib/user';
import StatusBadge from '@/components/StatusBadge';

export default function ApprovalsPage() {
  const approvals = useAsync(listApprovals, []);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function handleDecision(id: string, decision: 'approve' | 'reject') {
    const actingUser = getActingUser();
    if (!actingUser) {
      setActionError('Set your name (bottom of left nav) before approving or rejecting.');
      return;
    }
    setBusyId(id);
    setActionError(null);
    try {
      if (decision === 'approve') {
        await approveApproval(id, actingUser);
      } else {
        await rejectApproval(id, actingUser);
      }
      approvals.refetch();
    } catch (err: any) {
      setActionError(err?.message ?? 'API unreachable');
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div>
      <h1>Approval queue</h1>

      {actionError && <p className="error-banner">{actionError}</p>}
      {approvals.loading && <p className="hint">Loading…</p>}
      {approvals.error && <p className="error-banner">API unreachable — {approvals.error}</p>}
      {!approvals.loading && !approvals.error && (approvals.data?.length ?? 0) === 0 && (
        <p className="empty-state">No pending approvals.</p>
      )}

      <div className="card-grid">
        {approvals.data?.map((a: Approval) => (
          <div className="approval-card" key={a.id}>
            <div className="approval-card-header">
              <StatusBadge status={a.status} />
              <span className="risk-tag">{formatRiskLevel(a.riskLevel)}</span>
            </div>
            <h3>{a.action}</h3>
            <p className="approval-system">{a.system ?? 'system unspecified'}</p>

            {a.patient && (
              <dl className="intent-fields">
                <dt>Patient</dt>
                <dd>{patientDisplayName(a.patient)}</dd>
              </dl>
            )}

            {a.inputs && Object.keys(a.inputs).length > 0 && (
              <dl className="intent-fields">
                {Object.entries(a.inputs).map(([key, value]) => (
                  <InputRow key={key} name={key} value={value} />
                ))}
              </dl>
            )}

            {(a.requestedBy || a.createdAt) && (
              <p className="hint">
                Requested by {a.requestedBy ?? 'unknown'}
                {a.createdAt ? ` · ${new Date(a.createdAt).toLocaleString()}` : ''}
              </p>
            )}

            {a.status.toLowerCase() === 'pending' && (
              <div className="intent-actions" style={{ marginTop: 14 }}>
                <button onClick={() => handleDecision(a.id, 'approve')} disabled={busyId === a.id}>
                  Approve
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  onClick={() => handleDecision(a.id, 'reject')}
                  disabled={busyId === a.id}
                >
                  Reject
                </button>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function InputRow({ name, value }: { name: string; value: unknown }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd>
    </>
  );
}

'use client';

import { useParams } from 'next/navigation';
import { useAsync } from '@/lib/useAsync';
import { getActionRun, evidenceUrl, ActionRunHistoryEntry } from '@/lib/api';
import StatusBadge from '@/components/StatusBadge';

// The "happy path" the spec describes: proposed -> approved -> executing ->
// verified. A run that ends in failed / blocked / needs_human_review instead
// of verified is shown as the last stage in the timeline.
const HAPPY_PATH = ['proposed', 'approved', 'executing', 'verified'];
const ALT_TERMINALS = new Set(['failed', 'blocked', 'needs_human_review', 'success']);

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const run = useAsync(() => getActionRun(id), [id]);

  return (
    <div>
      <h1>Run {id}</h1>

      {run.loading && <p className="hint">Loading…</p>}
      {run.error && <p className="error-banner">API unreachable — {run.error}</p>}

      {run.data && (
        <>
          <section className="panel">
            <div className="run-summary">
              <div>
                <span className="hint">Action</span>
                <div>{run.data.action ?? '—'}</div>
              </div>
              <div>
                <span className="hint">System</span>
                <div>{run.data.system ?? '—'}</div>
              </div>
              <div>
                <span className="hint">Status</span>
                <div>
                  <StatusBadge status={run.data.status} />
                </div>
              </div>
              <div>
                <span className="hint">Verified</span>
                <div>{run.data.verified === undefined ? '—' : run.data.verified ? 'Yes' : 'No'}</div>
              </div>
            </div>
          </section>

          <section className="panel">
            <h2>Timeline</h2>
            <Timeline currentStatus={run.data.status} history={run.data.history} />
          </section>

          {(run.data.screenshotId || run.data.traceId) && (
            <section className="panel">
              <h2>Evidence</h2>
              <ul className="evidence-list">
                {run.data.screenshotId && (
                  <li>
                    <a href={evidenceUrl(run.data.screenshotId)} target="_blank" rel="noreferrer">
                      Screenshot evidence →
                    </a>
                  </li>
                )}
                {run.data.traceId && (
                  <li>
                    <a href={evidenceUrl(run.data.traceId)} target="_blank" rel="noreferrer">
                      Execution trace →
                    </a>
                  </li>
                )}
              </ul>
            </section>
          )}

          {(run.data.failureReason || (run.data.warnings && run.data.warnings.length > 0)) && (
            <section className="panel">
              <h2>Warnings &amp; failure detail</h2>
              {run.data.failureReason && <p className="error-banner">{run.data.failureReason}</p>}
              {run.data.warnings && run.data.warnings.length > 0 && (
                <ul>
                  {run.data.warnings.map((w, i) => (
                    <li key={i} className="hint">
                      {w}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}

function Timeline({
  currentStatus,
  history,
}: {
  currentStatus: string;
  history?: ActionRunHistoryEntry[];
}) {
  const key = (currentStatus || '').toLowerCase();
  const stages = ALT_TERMINALS.has(key)
    ? ['proposed', 'approved', 'executing', key]
    : HAPPY_PATH;
  const currentIndex = Math.max(
    stages.findIndex((s) => s === key),
    key === 'success' ? stages.length - 1 : -1
  );

  return (
    <div>
      <div className="run-timeline">
        {stages.map((stage, i) => (
          <div key={`${stage}-${i}`} className={`timeline-step${i <= currentIndex ? ' timeline-step-active' : ''}`}>
            <StatusBadge status={i === currentIndex ? currentStatus : stage} />
          </div>
        ))}
      </div>

      {history && history.length > 0 && (
        <ul className="timeline-history">
          {history.map((h, i) => (
            <li key={i}>
              <StatusBadge status={h.status} />
              <span className="hint">{h.at ? new Date(h.at).toLocaleString() : ''}</span>
              {h.note && <span>{h.note}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

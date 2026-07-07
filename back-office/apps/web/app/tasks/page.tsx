'use client';

import { useState } from 'react';
import { useAsync } from '@/lib/useAsync';
import {
  listTasks,
  parseIntent,
  createTask,
  submitForApproval,
  formatRiskLevel,
  patientDisplayName,
  ActionIntent,
  Task,
} from '@/lib/api';
import { getActingUser } from '@/lib/user';
import StatusBadge from '@/components/StatusBadge';

export default function TasksPage() {
  const tasks = useAsync(listTasks, []);

  const [text, setText] = useState('');
  const [intent, setIntent] = useState<ActionIntent | null>(null);
  const [parsing, setParsing] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);

  async function handleParse(e: React.FormEvent) {
    e.preventDefault();
    if (!text.trim()) return;
    setParsing(true);
    setParseError(null);
    setIntent(null);
    setSubmitted(false);
    try {
      const result = await parseIntent({ prompt: text.trim(), requestedBy: getActingUser() });
      setIntent(result.intent);
    } catch (err: any) {
      setParseError(err?.message ?? 'API unreachable');
    } finally {
      setParsing(false);
    }
  }

  async function handleSubmitForApproval() {
    if (!intent) return;
    const actingUser = getActingUser();
    if (!actingUser) {
      setSubmitError('Set your name (bottom of left nav) before submitting.');
      return;
    }
    setSubmitting(true);
    setSubmitError(null);
    try {
      const task = await createTask({ prompt: text.trim(), createdBy: actingUser });
      await submitForApproval({
        intent,
        taskId: task.id,
        submittedBy: actingUser,
        staffPrompt: text.trim(),
      });
      setIntent(null);
      setText('');
      setSubmitted(true);
      tasks.refetch();
    } catch (err: any) {
      setSubmitError(err?.message ?? 'API unreachable');
    } finally {
      setSubmitting(false);
    }
  }

  function handleDiscard() {
    setIntent(null);
    setParseError(null);
  }

  return (
    <div>
      <h1>Tasks</h1>

      <section className="panel">
        <h2>New task</h2>
        <form onSubmit={handleParse} className="new-task-form">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Describe the task in plain language, e.g. Reschedule John Doe's appointment to next Tuesday at 2pm"
            rows={4}
          />
          <button type="submit" disabled={parsing || !text.trim()}>
            {parsing ? 'Parsing…' : 'Parse intent'}
          </button>
        </form>

        {parseError && <p className="error-banner">API unreachable — {parseError}</p>}
        {submitted && !intent && <p className="hint">Submitted for approval.</p>}

        {intent && (
          <div className="intent-card">
            <h3>Proposed action</h3>
            <dl className="intent-fields">
              <dt>Action</dt>
              <dd>{intent.action}</dd>
              <dt>System</dt>
              <dd>{intent.system ?? '—'}</dd>
              <dt>Risk level</dt>
              <dd>{formatRiskLevel(intent.riskLevel)}</dd>
              <dt>Patient</dt>
              <dd>{patientDisplayName(intent.patient)}</dd>
              <dt>Missing fields</dt>
              <dd>
                {intent.missingFields && intent.missingFields.length > 0
                  ? intent.missingFields.join(', ')
                  : 'None'}
              </dd>
              <dt>Reason</dt>
              <dd>{intent.reason ?? '—'}</dd>
            </dl>

            {submitError && <p className="error-banner">{submitError}</p>}

            <div className="intent-actions">
              <button onClick={handleSubmitForApproval} disabled={submitting}>
                {submitting ? 'Submitting…' : 'Submit for approval'}
              </button>
              <button type="button" className="btn-secondary" onClick={handleDiscard}>
                Discard
              </button>
            </div>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>All tasks</h2>
        {tasks.loading && <p className="hint">Loading…</p>}
        {tasks.error && <p className="error-banner">API unreachable — {tasks.error}</p>}
        {!tasks.loading && !tasks.error && (tasks.data?.length ?? 0) === 0 && (
          <p className="empty-state">No tasks yet.</p>
        )}
        {(tasks.data?.length ?? 0) > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Description</th>
                <th>Status</th>
                <th>Created by</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {tasks.data!.map((t: Task) => (
                <tr key={t.id}>
                  <td>{t.title}</td>
                  <td>
                    <StatusBadge status={t.status} />
                  </td>
                  <td>{t.createdBy ?? '—'}</td>
                  <td>{t.createdAt ? new Date(t.createdAt).toLocaleString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

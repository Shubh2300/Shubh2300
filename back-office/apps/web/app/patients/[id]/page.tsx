'use client';

import { useParams } from 'next/navigation';
import { useAsync } from '@/lib/useAsync';
import { getPatient, patientFullName } from '@/lib/api';

export default function PatientDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const patient = useAsync(() => getPatient(id), [id]);

  return (
    <div>
      <h1>Patient</h1>

      {patient.loading && <p className="hint">Loading…</p>}
      {patient.error && <p className="error-banner">API unreachable — {patient.error}</p>}

      {patient.data && (
        <section className="panel">
          <h2>Demographics</h2>
          <dl className="intent-fields">
            <dt>Name</dt>
            <dd>{patientFullName(patient.data)}</dd>
            <dt>DOB</dt>
            <dd>{patient.data.dob ?? '—'}</dd>
            <dt>Phone</dt>
            <dd>{patient.data.phone ?? '—'}</dd>
            <dt>Email</dt>
            <dd>{patient.data.email ?? '—'}</dd>
            <dt>System IDs</dt>
            <dd>
              {patient.data.systemIds && Object.keys(patient.data.systemIds).length > 0
                ? Object.entries(patient.data.systemIds)
                    .map(([system, extId]) => `${system}: ${extId}`)
                    .join(', ')
                : '—'}
            </dd>
            <dt>Created</dt>
            <dd>{patient.data.createdAt ? new Date(patient.data.createdAt).toLocaleString() : '—'}</dd>
          </dl>
        </section>
      )}
    </div>
  );
}

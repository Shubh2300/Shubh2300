'use client';

import Link from 'next/link';
import { useAsync } from '@/lib/useAsync';
import { listPatients, patientFullName, Patient } from '@/lib/api';

export default function PatientsPage() {
  const patients = useAsync(listPatients, []);

  return (
    <div>
      <h1>Patients</h1>

      {patients.loading && <p className="hint">Loading…</p>}
      {patients.error && <p className="error-banner">API unreachable — {patients.error}</p>}
      {!patients.loading && !patients.error && (patients.data?.length ?? 0) === 0 && (
        <p className="empty-state">No patients found.</p>
      )}

      {(patients.data?.length ?? 0) > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>DOB</th>
              <th>Systems</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {patients.data!.map((p: Patient) => (
              <tr key={p.id}>
                <td>{patientFullName(p)}</td>
                <td>{p.dob ?? '—'}</td>
                <td>{p.systemIds ? Object.keys(p.systemIds).join(', ') : '—'}</td>
                <td>
                  <Link href={`/patients/${p.id}`}>View →</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

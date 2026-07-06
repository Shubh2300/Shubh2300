# Transfer Guide: Clinical Dashboard

This folder is the local Clinical Dashboard app. It is designed to run on a single trusted computer at `http://localhost:8000`.

## What to Copy

Use the sanitized zip created next to this project:

`Antigravity-clinical-dashboard-transfer.zip`

It includes the dashboard source code, local scripts, setup docs, test, and `.env.example`.

It intentionally does not include:

- `.env` secrets
- `service_account.json`
- logs
- screenshots
- local scratch data and patient database files
- generated patient folders

Those excluded files can contain credentials or PHI.

## Install on the Work Computer

1. Install Python 3.10 or newer.
2. Unzip `Antigravity-clinical-dashboard-transfer.zip`.
3. Open Terminal in the unzipped `Antigravity` folder.
4. Create your local environment file:

```bash
cp .env.example .env
```

5. Edit `.env` and add only the credentials approved for the work computer.
6. Start the dashboard:

```bash
python3 server.py
```

7. Open:

```text
http://localhost:8000
```

For auto-restart during office use:

```bash
./run_server.sh
```

## Optional Data Migration

The dashboard reads operational data from:

```text
~/.gemini/antigravity/scratch
```

If you need the exact same local patient dashboard data on the work computer, copy only the approved files from that scratch folder after confirming the destination computer is authorized for PHI. Common files are:

- `patient_database.json`
- `patient_notes.json`
- `surgery_statuses.json`
- `team_tasks.json`
- `billing_ledger.json`
- `staff.json`
- `followups.json`

You can also put those files in a different folder and set this in `.env`:

```bash
ANTIGRAVITY_SCRATCH_DIR=/absolute/path/to/approved/scratch/folder
```

## Verify

Run:

```bash
python3 tests/smoke_test.py
```

Expected result: hard checks pass. Some warnings are okay if optional integrations are not configured yet.

## Compliance Notes

- Treat copied scratch files, logs, screenshots, and generated folders as PHI unless proven otherwise.
- Do not email the zip if PHI is added later.
- Keep `.env` and service account files off shared drives unless access is locked down.
- Use vendor BAAs and audit logging before connecting real patient data to external AI APIs.

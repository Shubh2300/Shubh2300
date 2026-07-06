#!/usr/bin/env python3
"""
Regenerate ~/.gemini/antigravity/scratch/patients_data.js from the canonical
patient_database.json that lives in the same scratch directory.

Rules:
  - EXCEL_PATIENTS: every record with a non-empty name, all fields passed
    through FAITHFULLY (no invented/normalised statuses, transcripts, or values).
  - PATIENT_DETAILS: cannot be derived from the database; the existing
    patients_data.js is read and its window.PATIENT_DETAILS section is
    carried over verbatim. If no existing file exists, writes an empty object.
  - PHI — output goes to scratch only; never commit or copy into the repo.

Usage:
    python3 extract_patients_data.py
"""

import json
import os
from datetime import datetime

SCRATCH = os.path.expanduser("~/.gemini/antigravity/scratch")
DB_PATH = os.path.join(SCRATCH, "patient_database.json")
OUT_PATH = os.path.join(SCRATCH, "patients_data.js")

# ── Load patient database ─────────────────────────────────────────────────────
if not os.path.exists(DB_PATH):
    raise FileNotFoundError(
        f"patient_database.json not found at {DB_PATH}\n"
        "Run the SIS capture agent first to populate it."
    )

with open(DB_PATH, "r", encoding="utf-8") as fh:
    raw = json.load(fh)

# The database may be a list of records or a dict with a key like "patients".
if isinstance(raw, list):
    all_records = raw
elif isinstance(raw, dict):
    # Common shapes: {"patients": [...]} or {"data": [...]}
    if "patients" in raw:
        all_records = raw["patients"]
    elif "data" in raw:
        all_records = raw["data"]
    else:
        # Treat dict values as a flat record list (keyed by patient ID)
        all_records = list(raw.values())
else:
    raise ValueError(f"Unexpected database shape: {type(raw)}")

# Filter: keep only records with a non-empty name. Faithful pass-through only.
def has_name(rec):
    if not isinstance(rec, dict):
        return False
    for key in ("name", "Name", "patient_name", "PatientName", "full_name"):
        val = rec.get(key, "")
        if isinstance(val, str) and val.strip():
            return True
    return False

patients = [r for r in all_records if has_name(r)]
print(f"  Records with non-empty name: {len(patients):,} of {len(all_records):,} total")

# ── Carry over PATIENT_DETAILS verbatim from existing file ───────────────────
patient_details_block = "window.PATIENT_DETAILS = {};"

if os.path.exists(OUT_PATH):
    with open(OUT_PATH, "r", encoding="utf-8") as fh:
        existing = fh.read()

    marker = "window.PATIENT_DETAILS = {"
    idx = existing.find(marker)
    if idx != -1:
        # Slice from the marker to the end of file (the block ends with the
        # final '};' which is the last two characters of the file excluding
        # any trailing newline).
        patient_details_block = existing[idx:].rstrip()
        # Ensure it ends with '};'
        if not patient_details_block.endswith("};"):
            patient_details_block = patient_details_block.rstrip(";").rstrip("}").rstrip() + "};"
        print(f"  Carrying over PATIENT_DETAILS verbatim from existing patients_data.js")
    else:
        print("  No PATIENT_DETAILS found in existing file — writing empty object")
else:
    print("  No existing patients_data.js — writing empty PATIENT_DETAILS")

# ── Build JS output ───────────────────────────────────────────────────────────
today = datetime.now().strftime("%Y-%m-%d")
header = (
    f"// Generated from patient_database.json on {today}.\n"
    "// Regenerate with extract_patients_data.py.\n"
    "// PHI — never commit or copy into the repo.\n\n"
)

excel_patients_js = (
    "window.EXCEL_PATIENTS = "
    + json.dumps(patients, indent=1, ensure_ascii=False)
    + ";\n"
)

js_content = header + excel_patients_js + "\n" + patient_details_block + "\n"

# ── Write output ──────────────────────────────────────────────────────────────
with open(OUT_PATH, "w", encoding="utf-8") as fh:
    fh.write(js_content)

out_bytes = os.path.getsize(OUT_PATH)
print(f"  Output: {OUT_PATH}")
print(f"  patients: {len(patients):,}  |  output size: {out_bytes:,} bytes")

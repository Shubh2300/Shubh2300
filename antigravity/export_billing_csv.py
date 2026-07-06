#!/usr/bin/env python3
"""
export_billing_csv.py — Dump every patient's billing from the app to a CSV so you
can eyeball / spot-check the numbers in a spreadsheet (or diff against SIS/Svigg).

    python3 export_billing_csv.py

Writes ~/Downloads/app_billing_export.csv. Read-only on the database — safe to run
even while a sync is in progress (it's a point-in-time snapshot).
"""
import csv
import json
import os

DB_PATH = os.path.join(os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch")), "patient_database.json")
OUT = os.path.join(os.path.expanduser("~"), "Downloads", "app_billing_export.csv")

COLUMNS = ["Name", "DOB", "Type", "Insurance", "Attorney",
           "Surgery Status", "Billing Status", "Payments", "Balance",
           "Raw Status", "Source"]


def main():
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            patients = json.load(f)
    except Exception as e:
        print(f"Could not read database (it may be mid-write — try again in a moment): {e}")
        return

    rows = 0
    with_billing = 0
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for p in patients:
            b = p.get("billing") or {}
            pay = b.get("payments", "") or 0
            bal = b.get("balance", "") or 0
            if (pay and pay > 0) or (bal and bal > 0):
                with_billing += 1
            w.writerow([
                p.get("name", ""), p.get("dob", ""), p.get("type", ""),
                p.get("insurance", ""), p.get("attorney", ""),
                p.get("surgeryStatus", ""), b.get("status", ""),
                pay, bal, b.get("raw_status", ""), p.get("source", ""),
            ])
            rows += 1

    print(f"Wrote {rows} patients to {OUT}")
    print(f"  ({with_billing} have real billing data; the rest show 0 / no record)")


if __name__ == "__main__":
    main()

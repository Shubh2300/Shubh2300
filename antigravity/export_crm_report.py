#!/usr/bin/env python3
"""
export_crm_report.py — Report of every patient the crawler matched in SIS / Sunny Vigg
(or the billing ledger), with balances, payments, status, and notes. Use this to
diff against the spreadsheet. READ-ONLY.

    python3 export_crm_report.py   ->  ~/Downloads/sis_svigg_full_report.csv
"""
import csv, json, os

DB = os.path.join(os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch")), "patient_database.json")
OUT = os.path.join(os.path.expanduser("~"), "Downloads", "sis_svigg_full_report.csv")
COLS = ["Name", "DOB", "Type", "Insurance", "Attorney", "Billing Status",
        "Payments", "Balance", "Matched In", "Notes / Raw Status"]


def source_of(raw):
    r = (raw or "").lower()
    if "sunnyvigg" in r or "sunny vigg" in r: return "Sunny Vigg"
    if "ledger" in r:                          return "Ledger"
    if "sis" in r:                             return "SIS Complete"
    return "CRM"


def main():
    pts = json.load(open(DB, encoding="utf-8"))
    rows, collected, owed = [], 0.0, 0.0
    for p in pts:
        b = p.get("billing")
        if not isinstance(b, dict):
            continue
        raw = b.get("raw_status", "")
        pay = b.get("payments", 0) or 0
        bal = b.get("balance", 0) or 0
        # "matched" = anything that isn't the not-found default
        matched = "no active billing records" not in raw.lower() and (pay > 0 or bal > 0 or b.get("status") not in ("No Balance", "Not Found", "Unknown", None, ""))
        if not matched:
            continue
        note = raw
        if b.get("notes"): note += " | notes: " + str(b["notes"])
        if b.get("ledgerNotes"): note += " | ledgerNotes: " + str(b["ledgerNotes"])
        collected += pay; owed += bal
        rows.append([p.get("name", ""), p.get("dob", ""), p.get("type", ""),
                     p.get("insurance", ""), p.get("attorney", ""), b.get("status", ""),
                     pay, bal, source_of(raw), note])

    rows.sort(key=lambda r: -(r[6] + r[7]))  # biggest dollars first
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(COLS); w.writerows(rows)

    from collections import Counter
    print(f"Wrote {len(rows)} CRM-matched patients to {OUT}")
    print(f"  by source: {dict(Counter(r[8] for r in rows))}")
    print(f"  total payments: ${collected:,.2f}   total balances owed: ${owed:,.2f}")


if __name__ == "__main__":
    main()

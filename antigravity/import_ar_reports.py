#!/usr/bin/env python3
"""
import_ar_reports.py — load the SIS / Sunny Vigg AR report files into the app.

Scans ~/.gemini/antigravity/scratch/ar_reports/ for .csv/.xlsx files, auto-
detects the money columns by header keywords, matches rows to patients by
normalized name, and updates each patient's billing block with the real
numbers: billed, write-offs, payments, balance (+ source + as-of date).

This REPLACES the per-patient scraper guesses with statement-grade data and
never produces false zeros: a patient absent from every report keeps
status "No Data".

Usage:
  python3 import_ar_reports.py             # import newest file per source
  python3 import_ar_reports.py --dry-run   # show what would change
  python3 import_ar_reports.py path.xlsx   # import one specific file

Column auto-detection (case-insensitive, first match wins):
  name     : patient, name, account name, guarantor
  billed   : charge, billed, total charges, debits
  writeoff : write-off, writeoff, adjustment, adj
  payments : payment, paid, receipts, credits
  balance  : balance, due, outstanding, a/r, ar amount
If a file's headers don't auto-detect, add an override in
ar_reports/ar_import_config.json:
  {"<filename-substring>": {"name": "Account Name", "balance": "Total A/R"}}
"""

import csv
import json
import os
import re
import sys
from datetime import datetime

SCRATCH = os.environ.get("ANTIGRAVITY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity/scratch"))
AR_DIR = os.path.join(SCRATCH, "ar_reports")
DB_PATH = os.path.join(SCRATCH, "patient_database.json")
CONFIG_PATH = os.path.join(AR_DIR, "ar_import_config.json")

HEADER_KEYWORDS = {
    "name": ["patient name", "account name", "guarantor", "patient", "name"],
    "billed": ["total charge", "charges", "charge", "billed", "debit"],
    "writeoff": ["write-off", "write off", "writeoff", "adjustment", "adj"],
    "payments": ["payments", "payment", "paid", "receipt", "credit"],
    "balance": ["balance due", "open balance", "balance", "due", "outstanding", "a/r", "ar amount"],
}


def money(val):
    s = str(val or "").replace("$", "").replace(",", "").strip()
    if s.startswith("(") and s.endswith(")"):  # accounting negatives
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def name_keys(name):
    """Match keys tolerant of 'Last, First' vs 'First Last' and DOB suffixes."""
    n = str(name or "").lower()
    n = re.split(r"\bdob\b|\d{1,2}/\d{1,2}/\d{2,4}", n)[0]
    n = re.sub(r"[^a-z\s,]", "", n).strip()
    if not n:
        return set()
    keys = set()
    if "," in n:
        last, _, first = n.partition(",")
        words = (first.strip() + " " + last.strip()).split()
    else:
        words = n.split()
    if len(words) >= 2:
        keys.add("".join(words))
        keys.add("".join(reversed(words)))
        keys.add(words[0] + words[-1])
        keys.add(words[-1] + words[0])
    elif words:
        keys.add(words[0])
    return keys


def read_rows(path):
    """Yield rows as dicts from a CSV or XLSX file."""
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
            sample = f.read(4096)
            f.seek(0)
            dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
            reader = csv.reader(f, dialect)
            rows = list(reader)
    elif path.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = [[("" if c is None else c) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
    else:
        print(f"  skip (unsupported type): {path}")
        return []
    # find the header row: first row where >=2 keyword families match
    header_idx, mapping = None, {}
    for i, row in enumerate(rows[:20]):
        cells = [str(c).strip().lower() for c in row]
        m = {}
        for field, kws in HEADER_KEYWORDS.items():
            for j, cell in enumerate(cells):
                if any(kw in cell for kw in kws) and j not in m.values():
                    m[field] = j
                    break
        if "name" in m and len(m) >= 2:
            header_idx, mapping = i, m
            break
    if header_idx is None:
        print(f"  could not auto-detect headers in {os.path.basename(path)} — "
              f"add an override to {CONFIG_PATH}")
        return []
    # config overrides by filename substring
    if os.path.exists(CONFIG_PATH):
        cfg = json.load(open(CONFIG_PATH))
        for frag, overrides in cfg.items():
            if frag.lower() in os.path.basename(path).lower():
                header_cells = [str(c).strip().lower() for c in rows[header_idx]]
                for field, col_name in overrides.items():
                    if str(col_name).strip().lower() in header_cells:
                        mapping[field] = header_cells.index(str(col_name).strip().lower())
    print(f"  headers (row {header_idx + 1}): " +
          ", ".join(f"{f}->col{j + 1}" for f, j in mapping.items()))
    out = []
    for row in rows[header_idx + 1:]:
        rec = {}
        for field, j in mapping.items():
            rec[field] = row[j] if j < len(row) else ""
        if str(rec.get("name", "")).strip():
            out.append(rec)
    return out


def newest_files():
    if not os.path.isdir(AR_DIR):
        return []
    files = [os.path.join(AR_DIR, f) for f in os.listdir(AR_DIR)
             if f.lower().endswith((".csv", ".xlsx", ".xlsm")) and not f.startswith("~")]
    return sorted(files, key=os.path.getmtime, reverse=True)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry = "--dry-run" in sys.argv
    paths = args or newest_files()
    if not paths:
        sys.exit(f"No AR report files found in {AR_DIR}. "
                 "Run ar_export_agent.py (or download manually into that folder).")

    patients = json.load(open(DB_PATH))
    index = {}
    for p in patients:
        for k in name_keys(p.get("name")):
            index.setdefault(k, p)

    today = datetime.now().strftime("%Y-%m-%d")
    total_matched, total_unmatched, unmatched_names = 0, 0, []
    for path in paths:
        print(f"\nImporting {os.path.basename(path)}")
        rows = read_rows(path)
        print(f"  {len(rows)} data rows")
        for rec in rows:
            patient = None
            for k in name_keys(rec.get("name")):
                if k in index:
                    patient = index[k]
                    break
            vals = {f: money(rec.get(f)) for f in ("billed", "writeoff", "payments", "balance")}
            if all(v is None for v in vals.values()):
                continue  # subtotal/blank row
            if patient is None:
                total_unmatched += 1
                unmatched_names.append(str(rec.get("name")).strip())
                continue
            total_matched += 1
            if dry:
                continue
            b = patient.get("billing") or {}
            if vals["billed"] is not None:
                b["billed"] = vals["billed"]
            if vals["writeoff"] is not None:
                b["writeOffs"] = vals["writeoff"]
            if vals["payments"] is not None:
                b["payments"] = vals["payments"]
            if vals["balance"] is not None:
                b["balance"] = vals["balance"]
            bal, pay = b.get("balance"), b.get("payments")
            if bal is not None:
                if bal > 0:
                    b["status"] = "Owes Us"
                elif (pay or 0) > 0:
                    b["status"] = "Fully Paid"
                else:
                    b["status"] = "No Balance"
            b["dataMissing"] = False
            b["raw_status"] = f"AR report import {os.path.basename(path)} ({today})"
            b["asOf"] = today
            patient["billing"] = b

    if not dry:
        backup = DB_PATH.replace(".json", f".backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        os.rename(DB_PATH, backup)
        json.dump(patients, open(DB_PATH, "w"), indent=2)
        print(f"\nSaved. (previous DB kept at {os.path.basename(backup)})")
    print(f"\nMatched rows: {total_matched} | Unmatched: {total_unmatched}")
    if unmatched_names:
        out = os.path.join(AR_DIR, f"unmatched_{datetime.now().strftime('%Y%m%d')}.txt")
        with open(out, "w") as f:
            f.write("\n".join(sorted(set(unmatched_names))))
        print(f"Unmatched names written to {out} — these may be patients missing "
              "from the app entirely (the reconciliation agent's favorite food).")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
One-off migration: extract embedded patient data blocks from dashboard.html
into ~/.gemini/antigravity/scratch/patients_data.js, then replace the blocks
with lightweight stubs and insert a <script> tag to load the external file.

PHI NOTE: patients_data.js is PHI — it goes to scratch only, never the repo.
"""
import os
import re
import shutil
from datetime import datetime

# ── Paths ────────────────────────────────────────────────────────────────────
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(WORKSPACE, "dashboard.html")
SCRATCH = os.path.expanduser("~/.gemini/antigravity/scratch")
os.makedirs(SCRATCH, exist_ok=True)

TIMESTAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
BACKUP = os.path.join(SCRATCH, f"dashboard.html.backup-{TIMESTAMP}")
OUTPUT_JS = os.path.join(SCRATCH, "patients_data.js")

# ── Read dashboard.html ───────────────────────────────────────────────────────
print(f"Reading {DASHBOARD} …")
with open(DASHBOARD, "r", encoding="utf-8") as fh:
    src = fh.read()

old_bytes = len(src.encode("utf-8"))
old_lines = src.count("\n") + 1
print(f"  Original: {old_lines:,} lines / {old_bytes:,} bytes")

# ── Backup ────────────────────────────────────────────────────────────────────
print(f"Writing backup → {BACKUP}")
shutil.copy2(DASHBOARD, BACKUP)

lines = src.splitlines(keepends=True)

# ── Locate block A (EXCEL_PATIENTS) ──────────────────────────────────────────
A_OPEN_MARKER = "const EXCEL_PATIENTS = ["
a_open_indices = [i for i, ln in enumerate(lines) if A_OPEN_MARKER in ln]
assert len(a_open_indices) == 1, (
    f"Expected exactly 1 occurrence of '{A_OPEN_MARKER}', got {len(a_open_indices)}"
)
a_start = a_open_indices[0]  # 0-based line index

# Closing line: first line after start that is exactly '    ];'
a_close = None
for i in range(a_start + 1, len(lines)):
    if lines[i].rstrip("\n\r") == "    ];":
        a_close = i
        break
assert a_close is not None, "Could not find closing '    ];' for EXCEL_PATIENTS block"

# Body = lines between (exclusive) the opening line and closing line
# The opening line is `    const EXCEL_PATIENTS = [` — capture everything after '['
open_line_a = lines[a_start]
after_bracket_a = open_line_a[open_line_a.index("[") + 1:]  # text after '['
body_a_parts = [after_bracket_a] + lines[a_start + 1: a_close]
array_body = "".join(body_a_parts)
print(f"  Block A (EXCEL_PATIENTS): lines {a_start+1}–{a_close+1}")

# ── Locate block B (PATIENT_DETAILS) ─────────────────────────────────────────
B_OPEN_MARKER = "const PATIENT_DETAILS = {"
b_open_indices = [i for i, ln in enumerate(lines) if B_OPEN_MARKER in ln]
assert len(b_open_indices) == 1, (
    f"Expected exactly 1 occurrence of '{B_OPEN_MARKER}', got {len(b_open_indices)}"
)
b_start = b_open_indices[0]

# Closing line: first line after start that is exactly '    };'
b_close = None
for i in range(b_start + 1, len(lines)):
    if lines[i].rstrip("\n\r") == "    };":
        b_close = i
        break
assert b_close is not None, "Could not find closing '    };' for PATIENT_DETAILS block"

open_line_b = lines[b_start]
after_brace_b = open_line_b[open_line_b.index("{") + 1:]
body_b_parts = [after_brace_b] + lines[b_start + 1: b_close]
object_body = "".join(body_b_parts)
print(f"  Block B (PATIENT_DETAILS): lines {b_start+1}–{b_close+1}")

# ── Write patients_data.js ────────────────────────────────────────────────────
today = datetime.now().strftime("%Y-%m-%d")
js_header = (
    f"// Generated from dashboard.html on {today}.\n"
    "// Regenerate with extract_patients_data.py.\n"
    "// PHI — never commit or copy into the repo.\n\n"
)

js_content = (
    js_header
    + "window.EXCEL_PATIENTS = ["
    + array_body
    + "];\n\n"
    + "window.PATIENT_DETAILS = {"
    + object_body
    + "};\n"
)

print(f"Writing {OUTPUT_JS} …")
with open(OUTPUT_JS, "w", encoding="utf-8") as fh:
    fh.write(js_content)

js_bytes = len(js_content.encode("utf-8"))
print(f"  patients_data.js: {js_bytes:,} bytes")

# ── Build replacement stubs ───────────────────────────────────────────────────
# Preserve the leading whitespace of the original opening line
indent_a = len(open_line_a) - len(open_line_a.lstrip())
pad_a = " " * indent_a

stub_a = (
    f"{pad_a}// PATIENT DATA MOVED to ~/.gemini/antigravity/scratch/patients_data.js\n"
    f"{pad_a}// (served at /patients_data.js, loaded by the script tag before this block).\n"
    f"{pad_a}// Regenerate with extract_patients_data.py. Do NOT re-embed patient data\n"
    f"{pad_a}// in this file — it bloats every load and bakes PHI into git.\n"
    f"{pad_a}const EXCEL_PATIENTS = window.EXCEL_PATIENTS || [];\n"
)

indent_b = len(open_line_b) - len(open_line_b.lstrip())
pad_b = " " * indent_b

stub_b = (
    f"{pad_b}// Moved to patients_data.js — see note at EXCEL_PATIENTS.\n"
    f"{pad_b}const PATIENT_DETAILS = window.PATIENT_DETAILS || {{}};\n"
)

# ── Rebuild lines list with replacements ─────────────────────────────────────
# Replace block A (lines a_start..a_close inclusive) with stub_a
# Replace block B (lines b_start..b_close inclusive) with stub_b
# Work from the bottom up so indices stay valid.

new_lines = list(lines)

# Replace block B first (it's after A, so replace bottom-up)
new_lines[b_start: b_close + 1] = [stub_b]

# Now block A indices are still valid (B is after A)
new_lines[a_start: a_close + 1] = [stub_a]

# ── Insert <script src="/patients_data.js"> before the inline <script> tag ───
# Find the <script tag line that was immediately before (or at) the EXCEL_PATIENTS block
# Strategy: find the last occurrence of a line starting with '<script' before a_start.
# After the replacements above the indices shifted, so we search in new_lines.
# The stub_a replaced a range; new a_start is still the correct line index for stub_a.
# We need the <script tag that opens the inline block containing EXCEL_PATIENTS.
# That tag was the last <script opening tag before a_start in the ORIGINAL lines list.

script_tag_orig_idx = None
for i in range(a_start - 1, -1, -1):
    stripped = lines[i].strip()
    if stripped.startswith("<script") and not stripped.startswith("<script src"):
        script_tag_orig_idx = i
        break

assert script_tag_orig_idx is not None, "Could not find opening <script> tag before EXCEL_PATIENTS"

# In new_lines, after replacing block B (which is after A), block A replacement
# is a single line at position a_start. The script tag was before a_start so its
# position in new_lines is unchanged.
script_tag_new_idx = script_tag_orig_idx

# Build indent from the script tag line
script_line = new_lines[script_tag_new_idx]
script_indent = len(script_line) - len(script_line.lstrip())
pad_s = " " * script_indent

inject_line = f'{pad_s}<script src="/patients_data.js"></script>\n'

# Insert BEFORE the script tag line
new_lines.insert(script_tag_new_idx, inject_line)

# Verify exactly one such line was inserted (sanity check: count occurrences)
injected_count = sum(1 for ln in new_lines if '/patients_data.js">' in ln)
assert injected_count == 1, (
    f"Expected exactly 1 injected patients_data.js script tag, got {injected_count}"
)
print(f"  Inserted <script src='/patients_data.js'> before line {script_tag_new_idx+1}")

# ── Write modified dashboard.html ────────────────────────────────────────────
new_src = "".join(new_lines)
new_bytes = len(new_src.encode("utf-8"))
new_lines_count = new_src.count("\n") + 1

print(f"Writing modified {DASHBOARD} …")
with open(DASHBOARD, "w", encoding="utf-8") as fh:
    fh.write(new_src)

print()
print("=== MIGRATION COMPLETE ===")
print(f"  dashboard.html: {old_lines:,} lines / {old_bytes:,} bytes  →  "
      f"{new_lines_count:,} lines / {new_bytes:,} bytes")
print(f"  patients_data.js: {js_bytes:,} bytes")
print(f"  Backup: {BACKUP}")

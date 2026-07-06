#!/usr/bin/env python3
"""
restyle_inline.py — migrate inline style literals in dashboard.html
from dark glassmorphic values to light Clinical OS CSS vars.

Usage: python3 scripts/restyle_inline.py
Prints counts per mapping rule. Does NOT print file content.
"""

import re
import sys
from pathlib import Path

SRC = Path(__file__).parent.parent / "dashboard.html"
if not SRC.exists():
    print(f"ERROR: {SRC} not found", file=sys.stderr)
    sys.exit(1)

text = SRC.read_text(encoding="utf-8")
original_len = len(text)

counts = {}

def sub(pattern, replacement, s, rule_name, flags=re.IGNORECASE):
    global counts
    new_s, n = re.subn(pattern, replacement, s, flags=flags)
    counts[rule_name] = counts.get(rule_name, 0) + n
    return new_s

# ─── 1. Remove ALL backdrop-filter declarations (CSS and inline styles) ───────
text = sub(
    r'-webkit-backdrop-filter\s*:\s*[^;"\']*;?',
    '',
    text,
    '-webkit-backdrop-filter removal'
)
text = sub(
    r'backdrop-filter\s*:\s*[^;"\']*;?',
    '',
    text,
    'backdrop-filter removal'
)

# ─── 2. Remove animation: pulse glow effects on dots (keep static) ─────────────
# Only kill animation:pulse on elements likely to be dots/status indicators
# We do a targeted inline style kill for animation:pulse in inline styles
text = sub(
    r'(style="[^"]*?)animation\s*:\s*pulse[^;"\']*;?\s*',
    r'\1',
    text,
    'animation:pulse removal (inline)'
)

# ─── 3. Color: white / #fff / #ffffff → var(--text-main) ──────────────────────
# Only in inline style color: properties (not background)
text = sub(
    r'((?:^|;|\s)color\s*:\s*)(?:#fff(?:fff)?|white)\s*([";])',
    r'\1var(--text-main)\2',
    text,
    'color:white→text-main',
    flags=re.IGNORECASE | re.MULTILINE
)

# ─── 4. color: text-sub shades ────────────────────────────────────────────────
for hex_val in ['#e2e8f0', '#cbd5e1']:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--text-sub)\2',
        text,
        f'color:{hex_val}→text-sub',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 5. color: text-muted shades ──────────────────────────────────────────────
for hex_val in ['#94a3b8', '#64748b', '#475569', '#a5b4fc', '#93c5fd', '#6e6e73']:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--text-muted)\2',
        text,
        f'color:{hex_val}→text-muted',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 6. color: greens → var(--green) ─────────────────────────────────────────
green_hexes = [
    '#34d399', '#30d158', '#10b981', '#22c55e', '#2dd4bf',
    '#14b8a6', '#86efac', '#bbf7d0', '#6ee7b7', '#0d9488'
]
for hex_val in green_hexes:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--green)\2',
        text,
        f'color:{hex_val}→green',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 7. color: reds → var(--red) ──────────────────────────────────────────────
red_hexes = ['#f87171', '#ff453a', '#ef4444', '#fca5a5', '#ff8a80', '#d70015']
for hex_val in red_hexes:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--red)\2',
        text,
        f'color:{hex_val}→red',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 8. color: ambers → var(--amber) ─────────────────────────────────────────
amber_hexes = ['#fbbf24', '#fcd34d', '#ff9f0a', '#f59e0b', '#f97316']
for hex_val in amber_hexes:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--amber)\2',
        text,
        f'color:{hex_val}→amber',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 9. color: blues → var(--accent) ─────────────────────────────────────────
blue_hexes = ['#60a5fa', '#3b82f6', '#0a84ff', '#2563eb', '#38bdf8']
for hex_val in blue_hexes:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--accent)\2',
        text,
        f'color:{hex_val}→accent',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 10. color: violets → var(--violet) ──────────────────────────────────────
for hex_val in ['#c084fc', '#a78bfa']:
    text = sub(
        rf'((?:^|;|\s)color\s*:\s*){re.escape(hex_val)}\s*([";])',
        r'\1var(--violet)\2',
        text,
        f'color:{hex_val}→violet',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── 11. background surfaces: rgba(255,255,255, low alpha) → --panel-2 ────────
# alpha ≤ 0.12 → panel-2 (not visible enough to matter as a real color)
text = sub(
    r'background(?:-color)?\s*:\s*rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*0\.\s*0[0-9]\d*\s*\)',
    'background: var(--panel-2)',
    text,
    'bg:rgba(255,255,255,<0.12)→panel-2'
)
# alpha 0.10 - 0.12
text = sub(
    r'background(?:-color)?\s*:\s*rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*0\.1[012]\s*\)',
    'background: var(--panel-2)',
    text,
    'bg:rgba(255,255,255,0.10-0.12)→panel-2'
)

# border rgba(255,255,255,*) → border-subtle
text = sub(
    r'border(?:-color)?\s*:\s*rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*[0-9.]+\s*\)',
    'border-color: var(--border-subtle)',
    text,
    'border:rgba(255,255,255,*)→border-subtle'
)

# dark bg surfaces rgba(0,0,0,0.18-0.5) → panel-2
text = sub(
    r'background(?:-color)?\s*:\s*rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*(?:0\.[1-4][0-9]*|0\.5)\s*\)',
    'background: var(--panel-2)',
    text,
    'bg:rgba(0,0,0,0.18-0.5)→panel-2'
)

# ─── 12. Tinted green rgba families ──────────────────────────────────────────
# Green RGB tuples: (16,185,129) (52,211,153) (48,209,88) (34,197,94) (20,184,166) (45,212,191)
green_rgbs = [
    r'16\s*,\s*185\s*,\s*129',
    r'52\s*,\s*211\s*,\s*153',
    r'48\s*,\s*209\s*,\s*88',
    r'34\s*,\s*197\s*,\s*94',
    r'20\s*,\s*184\s*,\s*166',
    r'45\s*,\s*212\s*,\s*191',
]
for rgb in green_rgbs:
    text = sub(
        rf'background(?:-color)?\s*:\s*rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'background: var(--green-soft)',
        text,
        'bg:green-rgba→green-soft'
    )
    text = sub(
        rf'border(?:-color|-top|-bottom|-left|-right)?\s*:\s*(?:[0-9]+px\s+\w+\s+)?rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'border-color: var(--green-border)',
        text,
        'border:green-rgba→green-border'
    )

# ─── 13. Tinted red rgba families ────────────────────────────────────────────
red_rgbs = [
    r'248\s*,\s*113\s*,\s*113',
    r'255\s*,\s*69\s*,\s*58',
    r'239\s*,\s*68\s*,\s*68',
]
for rgb in red_rgbs:
    text = sub(
        rf'background(?:-color)?\s*:\s*rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'background: var(--red-soft)',
        text,
        'bg:red-rgba→red-soft'
    )
    text = sub(
        rf'border(?:-color|-top|-bottom|-left|-right)?\s*:\s*(?:[0-9]+px\s+\w+\s+)?rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'border-color: var(--red-border)',
        text,
        'border:red-rgba→red-border'
    )

# ─── 14. Tinted amber rgba families ──────────────────────────────────────────
amber_rgbs = [
    r'251\s*,\s*191\s*,\s*36',
    r'255\s*,\s*159\s*,\s*10',
    r'245\s*,\s*158\s*,\s*11',
    r'249\s*,\s*115\s*,\s*22',
]
for rgb in amber_rgbs:
    text = sub(
        rf'background(?:-color)?\s*:\s*rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'background: var(--amber-soft)',
        text,
        'bg:amber-rgba→amber-soft'
    )
    text = sub(
        rf'border(?:-color|-top|-bottom|-left|-right)?\s*:\s*(?:[0-9]+px\s+\w+\s+)?rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'border-color: var(--amber-border)',
        text,
        'border:amber-rgba→amber-border'
    )

# ─── 15. Tinted blue rgba families → accent-soft ─────────────────────────────
blue_rgbs = [
    r'59\s*,\s*130\s*,\s*246',
    r'96\s*,\s*165\s*,\s*250',
    r'10\s*,\s*132\s*,\s*255',
]
for rgb in blue_rgbs:
    text = sub(
        rf'background(?:-color)?\s*:\s*rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'background: var(--accent-soft)',
        text,
        'bg:blue-rgba→accent-soft'
    )
    text = sub(
        rf'border(?:-color|-top|-bottom|-left|-right)?\s*:\s*(?:[0-9]+px\s+\w+\s+)?rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'border-color: #bfe0d4',
        text,
        'border:blue-rgba→accent-border'
    )

# ─── 16. Tinted violet rgba families ─────────────────────────────────────────
violet_rgbs = [
    r'168\s*,\s*85\s*,\s*247',
    r'167\s*,\s*139\s*,\s*250',
    r'139\s*,\s*92\s*,\s*246',
    r'129\s*,\s*140\s*,\s*248',
]
for rgb in violet_rgbs:
    text = sub(
        rf'background(?:-color)?\s*:\s*rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'background: var(--violet-soft)',
        text,
        'bg:violet-rgba→violet-soft'
    )
    text = sub(
        rf'border(?:-color|-top|-bottom|-left|-right)?\s*:\s*(?:[0-9]+px\s+\w+\s+)?rgba\(\s*{rgb}\s*,\s*[0-9.]+\s*\)',
        'border-color: var(--violet-border)',
        text,
        'border:violet-rgba→violet-border'
    )

# ─── 17. Neutral rgba (148,163,184) → neutral-soft / border ──────────────────
text = sub(
    r'background(?:-color)?\s*:\s*rgba\(\s*148\s*,\s*163\s*,\s*184\s*,\s*[0-9.]+\s*\)',
    'background: var(--neutral-soft)',
    text,
    'bg:neutral-rgba→neutral-soft'
)
text = sub(
    r'border(?:-color|-top|-bottom|-left|-right)?\s*:\s*(?:[0-9]+px\s+\w+\s+)?rgba\(\s*148\s*,\s*163\s*,\s*184\s*,\s*[0-9.]+\s*\)',
    'border-color: var(--border)',
    text,
    'border:neutral-rgba→border'
)

# ─── 18. Glow box-shadows → var(--shadow) ────────────────────────────────────
# box-shadow with rgba bright color glow (non-black) → var(--shadow)
text = sub(
    r'box-shadow\s*:\s*0\s+0\s+\d+px\s+(?:rgba\(\s*(?:16|52|48|34|20|45|59|96|10|139|168|251|255|245)\s*,)[^;"\n]+;?',
    'box-shadow: var(--shadow);',
    text,
    'glow-box-shadow→shadow'
)

# ─── 19. Update SVG chevron stroke from white to muted ────────────────────────
text = sub(
    r'stroke%3D%27%23ffffff%27',
    "stroke%3D%27%236e6e73%27",
    text,
    'svg-chevron-white→muted'
)
text = sub(
    r'stroke%3D\'%23ffffff\'',
    "stroke%3D'%236e6e73'",
    text,
    'svg-chevron-white2→muted'
)
# Also the %2364748b (slate) version → %236e6e73 already correct but let's ensure
text = sub(
    r'stroke%3D%22%23ffffff%22',
    'stroke%3D%22%236e6e73%22',
    text,
    'svg-chevron-white3→muted'
)

# ─── 20. Remaining high-contrast inline dark surfaces ────────────────────────
# background: #0a0a0a / #060912 / #0b0f19 → panel-2
for dark in ['#0a0a0a', '#060912', '#0b0f19', '#0d1623', '#111d2e', '#162032']:
    text = sub(
        rf'(background(?:-color)?\s*:\s*){re.escape(dark)}\s*([;"])',
        r'\1var(--panel-2)\2',
        text,
        f'bg:{dark}→panel-2',
        flags=re.IGNORECASE | re.MULTILINE
    )

# ─── Write back ───────────────────────────────────────────────────────────────
SRC.write_text(text, encoding="utf-8")

print("=" * 60)
print("restyle_inline.py — substitution counts")
print("=" * 60)
total = 0
for rule, count in sorted(counts.items(), key=lambda x: -x[1]):
    if count > 0:
        print(f"  {count:5d}  {rule}")
        total += count
print("-" * 60)
print(f"  {total:5d}  TOTAL substitutions")
print(f"  File size: {original_len:,} → {len(text):,} bytes")
print("=" * 60)

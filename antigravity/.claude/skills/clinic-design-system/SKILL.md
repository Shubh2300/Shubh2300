---
name: clinic-design-system
description: The exact visual design system for THIS project's dashboard.html — the dark, monochrome "Clinic AI Operations Board" for Atlantic Pain & Wellness. Use this whenever you add, edit, or restyle ANY UI in dashboard.html — cards, stat cards, badges, tables, nav pills, drawers, buttons, modals, colors, spacing, or typography. It defines the real CSS custom-property tokens, the type system (Inter + Fira Code), the component conventions, and the house rules (pure-black background, white/grey shell, color reserved strictly for clinical status). Always consult this BEFORE writing dashboard CSS or markup so new UI matches the existing look instead of drifting. Pairs with the ui-ux-pro-max skill: that one has the general UX rulebook, this one is the project-specific source of truth — when they disagree, this wins for dashboard.html.
---

# Clinic Design System (dashboard.html)

The dashboard is a **single-file, dark, monochrome operations board**. The shell is black/white/grey on purpose; color is a signal, not decoration. Restraint is the aesthetic — when something looks "off," it's usually because it added color, blur, or a radius the system doesn't use.

> Scope: this is the truth for `dashboard.html`. For general UX rules (accessibility, motion timing, form patterns) use **ui-ux-pro-max**. For JS structure and performance use **frontend-code-quality**.

## Design tokens — use these, never raw values

These live in `:root` at the top of `dashboard.html` (~line 12). **Always reference the token** (`var(--card-bg)`); never paste a raw hex/rgba into a component.

```css
--bg: #000000;                       /* page background — pure black */
--bg-2: #050505;                     /* secondary surface */
--card-bg: rgba(18, 18, 18, 0.88);   /* default card surface */
--card-border: rgba(255,255,255,0.08);
--card-border-hover: rgba(255,255,255,0.16);
--text-main: #ffffff;                /* primary text */
--text-sub: #cbd5e1;                 /* secondary text */
--text-muted: #94a3b8;               /* tertiary / labels */
--accent-blue: #ffffff;              /* intentionally white (see Color philosophy) */
--accent-purple: #ffffff;            /* intentionally white */
--accent-green: #30d158;             /* status: positive / paid / seen / online */
--accent-red: #ff453a;               /* status: denied / overdue / offline */
--accent-amber: #ff9f0a;             /* status: pending / attention */
--glass-blur: none;                  /* glass blur is OFF — do not reintroduce casually */
--font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
--radius-card: 28px;
--radius-sm: 16px;
--transition: 0.2s cubic-bezier(0.16, 1, 0.3, 1);
```

## Color philosophy (the most-violated rule)

The shell is **monochrome**. Note `--accent-blue` and `--accent-purple` are deliberately set to `#ffffff` — earlier colored accents were collapsed to white on purpose. So:

- **Structure** (cards, borders, text, nav, icons) → black / white / grey only.
- **Color is reserved for clinical status**, and only these three:
  - `--accent-green` → paid, seen, on-track, agent online, success
  - `--accent-red` → denied, overdue, missing-critical, agent offline, destructive
  - `--accent-amber` → pending, needs-attention, awaiting auth
- Don't introduce a new brand hue (blue, purple, teal…) to "spice up" a section. If you need emphasis without status meaning, use weight, size, spacing, or border, not a new color.

## Typography

- **Inter** for all UI text (`var(--font-sans)`). Weights already loaded: 300–800.
- **Fira Code** for logs, the live console, and monospaced/numeric readouts. Use it for data that benefits from **tabular figures** (counts, money, timers) so columns don't jitter.
- Hierarchy via weight + size, not color: bold (600–700) headings, 400 body, 500 labels.

## Radii, spacing, motion

- Cards use `--radius-card` (28px); small chips/inputs use `--radius-sm` (16px). Don't invent in-between radii.
- Keep a consistent spacing rhythm (the CSS already uses an 8px-ish scale). Match neighbors.
- Transitions use `--transition`. Keep micro-interactions ~150–250ms. **Performance:** a recent commit removed excessive layer promotions on cards/stat-cards — don't add `will-change` / `transform: translateZ(0)` to list items or repeated cards (see frontend-code-quality).

## Reuse these components (don't reinvent)

The stylesheet is organized into banner-commented sections. Before adding a new style, reuse one of these:

| Class | Use for |
|---|---|
| `.card`, `.card-header-row`, `.card-title`, `.card-subtitle` | Any panel/section container |
| `.stat-card` | Top-line KPI / metric tiles |
| `.badge` + `.badge-new` `.badge-wc` `.badge-pending` `.badge-success` | Status pills on patients/cases (new intake, workers' comp, pending, done) |
| `.nav-pill` (in `.nav-pills`) | Top navigation tabs |
| `.table-container` | Patient/data tables |
| `.drawer-*` (`.drawer-header`, `.drawer-title`, `.drawer-section-title`, `.drawer-card`, `.drawer-next-action`…) | The slide-in patient detail drawer |
| `.btn-trigger` | Primary action buttons |
| `.agent-*` (`.agent-row-card`, `.agent-pulse-dot`, `.agent-health-banner`…) | The AI agent network / health widgets |

If you genuinely need a new component, **add a new banner-commented section** in the same style as the existing ones, build it from the tokens above, and keep naming consistent (`.thing`, `.thing-header`, `.thing-title`).

## House rules — do / don't

- ✅ Reference tokens; ❌ hardcode hex/rgba in a component.
- ✅ Black/white/grey shell; ❌ new brand colors. Color = clinical status only.
- ✅ Reuse `.card` / `.badge` / `.stat-card`; ❌ bespoke one-off card styles.
- ✅ SVG/inline icons; ❌ emoji as structural icons (a cleanup pass already stripped emoji from the app — keep it that way).
- ✅ Dark-only — there is no light mode today; don't add light-mode variants unless asked.
- ✅ `--glass-blur` is `none`; ❌ don't sprinkle `backdrop-filter: blur()` — it was deliberately turned off (perf + clarity).
- ✅ Escape any patient-derived text before inserting into markup (see frontend-code-quality `escapeHTML`).

## Extending the system, step by step

1. Need a value? Check tokens first. If it's truly new and reusable, **add a token** to `:root`, then use it.
2. Need a component? Reuse the table above. If not, add a banner section, build from tokens, name consistently.
3. Adding status meaning? Map to green/red/amber — don't invent a color.
4. Sanity check against **ui-ux-pro-max** (`--domain ux`) for accessibility/contrast before finishing.

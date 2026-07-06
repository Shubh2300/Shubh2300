---
name: frontend-code-quality
description: How to write and edit the frontend of THIS project without making the big single-file dashboard worse. Use this whenever you add a feature, fix a bug, or refactor in dashboard.html — its vanilla JS, its HTML, or its event wiring. dashboard.html is one ~9,400-line file (CSS, then HTML, then a large vanilla-JS script with no framework or build step). This skill captures the conventions already in the file (naming, fetch/API_BASE, escapeHTML for patient data, polling, localStorage stores), the render-performance practices, and the maintainability rules so changes stay consistent and safe. Consult it before editing dashboard.html so new code reads like the surrounding code instead of fighting it.
---

# Frontend Code Quality (dashboard.html)

`dashboard.html` is a **single ~9,400-line file**: CSS (~lines 11–2049), HTML, then one big **vanilla-JS** `<script>` (~2834 onward). No framework, no bundler, no TypeScript. The bar is simple: **new code reads exactly like the code around it.** Don't introduce React/Vue/Tailwind/build tooling — match what's here.

> For visual tokens/components use **clinic-design-system**. For UX rules use **ui-ux-pro-max**. This skill is about JS/markup quality.

## Conventions already in the file — follow them

- **Naming by verb:** `render*` (build DOM), `refresh*` (re-fetch + re-render), `toggle*`, `open*`/`close*`, `mark*`, `update*`. Match this when adding functions (e.g., `renderDenialQueue`, not `denialQueueBuilder`).
- **Backend calls:** use the `API_BASE` constant + `fetch(\`${API_BASE}/api/...\`)`. Never hardcode `http://localhost:8000`. Existing endpoints include `/api/patients`, `/api/tasks` (+`/add`,`/update`), `/api/monday-meeting` (+`/log`), `/api/koko`, `/api/agents/health`, `/api/logs`, `/api/update-status`, `/api/webedoctor/sync`, `/api/sis/status`.
- **Polling:** background freshness uses `start*Polling()` with intervals (`startAgentHealthPolling`, `startLogPolling`). Reuse this pattern; clear intervals you create; don't stack duplicate pollers.
- **Local state:** per-patient notes etc. use `localStorage` via `getXStore()` / `saveXStore()` helpers (e.g., `getPatientNoteStore`). Reuse a store helper rather than calling `localStorage` directly. ⚠️ Known limitation: `localStorage` is **per-browser** — it does not sync across workstations. The plan is to move running notes to the backend; don't add *new* shared-state features on `localStorage` if they need to be seen by other staff.

## Security: escape patient-derived text — always

This app renders **patient data**. There is an `escapeHTML()` helper. **Any** value that originates from patient/referral/user data and is inserted via `innerHTML` or a template string MUST be wrapped in `escapeHTML()`. Building a row, a badge label, a drawer field, a note? Escape it. This is XSS prevention on real PHI — treat it as mandatory, not optional. Prefer `textContent` when you don't need markup.

## Render performance

A recent commit removed *excessive layer promotions* on cards/stat-cards. Keep that win:

- **Don't** add `will-change`, `transform: translateZ(0)`, or per-item `transition: all` to repeated list items / many cards — it creates a layer per node and bloats memory/compositing.
- **Batch DOM work:** build a string or `DocumentFragment` and write once, rather than appending in a loop inside the live tree (avoid read→write→read layout thrashing).
- For long lists (50+ rows), prefer simple rendering; consider windowing only if it actually janks. Re-render on data change, not on every minor event.
- Debounce/throttle high-frequency handlers (scroll, input, resize).

## Maintainability rules

- **Reuse, don't duplicate.** Before writing a helper, search for an existing one (`render*`, `escapeHTML`, store helpers, filter helpers like `matchesFilters`). The file already has a lot — grep first.
- **Keep functions small and single-purpose**, like the existing ones.
- **No placeholder `alert(...)` for "real" actions.** Several legacy `alert()` stubs remain (older intake/archive/bot-control sections). When you add an action, wire it to a real `/api/...` endpoint or render an explicit "not connected" disabled state — don't add new fake alerts.
- **Match the structure:** CSS additions get a banner-comment section (see clinic-design-system); JS additions sit near related functions, not bolted at the bottom.
- **Escape + tokens:** every injected value escaped; every style via design tokens.

## When the single file becomes the problem

It's already ~9,400 lines. You don't need to split it for small changes, but if you're adding a substantial new surface, it's reasonable to propose extracting JS into a `dashboard.js` (and CSS into `dashboard.css`) loaded by the page — keeping the same vanilla, no-build approach. Raise it with the user before doing a big split; don't do it silently mid-feature.

## Quick checklist

- [ ] New functions follow `render*/refresh*/toggle*/open*/mark*` naming.
- [ ] Backend via `API_BASE` + `fetch`, not hardcoded URLs.
- [ ] Every patient-derived string escaped (`escapeHTML`) or set via `textContent`.
- [ ] No new layer-promotion / `will-change` on repeated nodes; DOM writes batched.
- [ ] No new placeholder `alert()` — real endpoint or explicit disabled state.
- [ ] Reused existing helpers/components instead of duplicating.

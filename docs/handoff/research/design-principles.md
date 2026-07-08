# Design principles — avoiding "AI slop"

From a session discussion prompted by a TriFetch screenshot. Not acted on
yet (web/UI work is paused per the owner's instruction) — captured here so
it isn't lost by the time it's needed.

## The trigger: what TriFetch actually does well

The owner shared a TriFetch screenshot: an agent sidebar, a live browser pane
showing the agent actively driving eClinicalWorks, a patient-data panel, and
an approval card (Approve/Reject) for a pending write. The observation: it
looks clean and professional, "not AI slop" — except the screenshots in the
middle, which are literally live views of the browser automation running.

**The key insight:** that live browser pane isn't decoration — it's honest,
functional transparency. "Here is proof of exactly what the agent is doing
right now." That's the same principle behind this platform's own
screenshot-proof-on-every-action requirement (`DECISIONS.md` #13). Good
tools show real work happening; slop shows fake work, or no work, dressed up.

## What separates "AI slop" from professional/clean

| AI slop | Clean/professional |
|---|---|
| Color used everywhere for decoration (gradients, rainbow cards) | Color reserved **only** for status/meaning |
| Sparse "hero" dashboard: 3 huge stat numbers, lots of empty space | Dense, real information — lists, tables, key/value data |
| Generic rounded gradient buttons, glassmorphism, drop-shadows everywhere | Flat, low-chrome UI — borders and spacing do the work |
| Mismatched icon sets, emoji as UI elements | One consistent, simple icon set, used sparingly |
| Decorative animation (fade-ins, bouncy transitions for no reason) | Motion only when it communicates state (a "Running" badge, a live count) |
| Marketing copy in a work tool ("Welcome to your Dashboard! ✨") | Plain, operational language — "1 pending," "Awaiting Approval" |
| Ad-hoc spacing/padding, inconsistent type sizes | One spacing scale, 1–2 fonts, clear hierarchy |
| Fake/placeholder data that "feels" generated | Every number honest — never a fabricated field |

**The short version:** boring, dense, and honest reads as professional.
Decorative and sparse reads as AI slop.

## Decision already locked (see `DECISIONS.md` #2 / `antigravity`'s
`clinic-design-system` skill)

Dark monochrome ops-board look, color reserved strictly for clinical status
— chosen over TriFetch's brighter, more colorful branded look. The
recommendation from this discussion: **steal TriFetch's transparency
pattern** (show the real action happening, live), **not its color palette**
— the two aren't in conflict; a dark ops-board can still show a live proof
pane, it just doesn't need gradients to do it.

## When web work resumes

This should be read alongside `.claude/skills/ui-ux-pro-max/` (design
reference library) and `antigravity/.claude/skills/clinic-design-system/`
(the existing, already-in-production visual system for this same clinic) —
not reinvented from scratch, but also not literally copied in, per the 2.0
rewrite rule.

/* ===================================================================
   Atlantic Pain & Wellness · Head Injury Institute — Back Office
   Single-page vanilla JS. No build step, no external deps.

   Honesty rules honored in the UI:
     - Missing data renders "—", never invented values.
     - EMR execution results (incl. warnings) are shown VERBATIM; the UI
       never fabricates a success message the backend didn't return.
     - No PHI is written to console.log (only non-PHI status strings).
   Endpoints + JSON shapes follow the shared contract exactly.

   XSS posture: this file NEVER interpolates untrusted (PHI) values into
   innerHTML. Dynamic values are placed with textContent / DOM nodes only.
   innerHTML is used solely for STATIC markup skeletons; data is filled in
   afterward via text() / textContent. esc() remains as defense-in-depth.
   =================================================================== */
(() => {
  "use strict";

  const DASH = "—";

  /* ---------------- DOM helpers ---------------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /** Escape untrusted text (defense-in-depth; we prefer textContent). */
  function esc(v) {
    if (v === null || v === undefined || v === "") return DASH;
    return String(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Value-or-dash (returns raw string for use with textContent). */
  function val(v) {
    return v === null || v === undefined || v === "" ? DASH : String(v);
  }

  function el(tag, cls) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    return node;
  }

  /** el + textContent in one shot (safe: no HTML parsing). */
  function txt(tag, cls, text) {
    const node = el(tag, cls);
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /** Small labeled line: <span class="k">label</span> + value node. */
  function kv(k, v, kClass, vClass) {
    const frag = document.createDocumentFragment();
    frag.appendChild(txt("span", kClass || "k", k));
    frag.appendChild(document.createTextNode(" "));
    frag.appendChild(txt("span", vClass || "v", val(v)));
    return frag;
  }

  function fmtTs(ts) {
    if (!ts) return DASH;
    const d = new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    return d.toLocaleString([], {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  }

  function titleCase(s) {
    return String(s || "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  /**
   * Relative "time ago" from a timestamp — used in card heads and the
   * summary panel. Falls back to the absolute time when the value can't be
   * parsed, and to "—" when absent (never fabricates a time).
   */
  function relTime(ts) {
    if (!ts) return DASH;
    const d = new Date(ts);
    const t = d.getTime();
    if (isNaN(t)) return String(ts);
    const secs = Math.round((Date.now() - t) / 1000);
    if (secs < 45) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 60) return mins + "m ago";
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + "h ago";
    const days = Math.round(hrs / 24);
    if (days < 30) return days + "d ago";
    return fmtTs(ts);
  }

  /* Pure-plumbing param keys — internal wiring, never shown in the tidy view.
     They remain available in the raw "Details" disclosure for auditability. */
  const PLUMBING_KEYS = new Set([
    "source_message_id", "source_external_id", "external_id",
    "message_id", "thread_ref", "rowid", "row_id", "idempotency_key",
  ]);

  /* Essential fields to surface per action type (order matters). Anything
     not listed here for a known action still appears in raw Details. */
  const ESSENTIAL_FIELDS = {
    book_appointment: ["patient", "patient_name", "date", "time", "provider", "location"],
    reschedule_appointment: ["patient", "patient_name", "new_date", "new_time", "date", "time", "provider"],
    cancel_appointment: ["patient", "patient_name", "date", "time", "provider", "reason"],
    send_sms: ["to", "from", "from_number", "body", "message"],
    send_email: ["to", "from", "subject", "body"],
    create_patient: ["patient_name", "first_name", "last_name", "dob", "phone"],
    manual_task: ["title", "assignee", "due", "detail"],
  };
  /* Keys rendered as a full-width message block (long free text). */
  const MESSAGE_KEYS = new Set(["body", "message", "detail"]);

  /* ---------------- Toasts ---------------- */
  function toast(msg, kind = "") {
    const host = $("#toast-host");
    const t = txt("div", "toast" + (kind ? " " + kind : ""), msg);
    host.appendChild(t);
    setTimeout(() => t.remove(), 3800);
  }

  /* ---------------- fetch wrapper ----------------
     On 401 -> drop to login view and throw a sentinel we swallow. */
  class Unauthorized extends Error {}

  async function api(path, { method = "GET", body } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        credentials: "same-origin",
      });
    } catch (netErr) {
      // Network-level failure; never log PHI, only the path.
      console.warn("[net] request failed:", path);
      throw new Error("Network error — is the server running?");
    }

    if (res.status === 401) {
      showLogin();
      throw new Unauthorized("session expired");
    }

    let data = null;
    const text = await res.text();
    if (text) {
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
    }

    if (!res.ok) {
      const detail =
        (data && (data.detail || data.error || data.message)) ||
        `HTTP ${res.status}`;
      throw new Error(typeof detail === "string" ? detail : "Request failed");
    }
    return data;
  }

  /* ================================================================
     APP STATE
     ================================================================ */
  const state = {
    user: null,          // {username, role}
    view: "home",
    chat: [],            // [{role, content}]  (client-side scrollback)
    chatBusy: false,
    inboxSelected: null, // id of message shown in the detail pane
    inboxRows: [],       // last-loaded message rows (full objects incl. body)
    // IDs of cards with a decision request in flight — the poll loop must not
    // re-render (and thus reset) a card the user is actively deciding on.
    decidingIds: new Set(),
    // IDs optimistically removed after a local approve/deny; kept out of lists
    // until the backend confirms on the next poll (then the set is rebuilt
    // from real state — we never keep a stale card alive contrary to backend).
    optimisticGone: new Set(),
    pollTimer: null,     // interval handle for the live-sync loop
    polling: false,      // re-entrancy guard for an in-flight poll
  };

  /* ================================================================
     VIEW ROUTING
     ================================================================ */
  const VIEWS = ["home", "chat", "approvals", "referrals", "inbox"];

  function showLogin() {
    // A 401 (session expired / backend restart) lands here from the fetch
    // wrapper. Halt the ~6s live-poll loop and clear auth/session state, else
    // state.pollTimer keeps firing /api/home every 6s forever behind the login
    // screen (each 401 re-invoking showLogin), and a stale pre-expiry
    // state.user leaks into the next login. Nulling state.user also makes the
    // `if (!state.user) return` guard in pollTick actually effective (audit #10).
    stopPolling();
    state.user = null;
    state.decidingIds.clear();
    state.optimisticGone.clear();
    $("#app-shell").hidden = true;
    $("#view-login").hidden = false;
    $("#login-password").value = "";
  }

  function showApp() {
    $("#view-login").hidden = true;
    $("#app-shell").hidden = false;
  }

  function setView(name) {
    if (!VIEWS.includes(name)) name = "home";
    state.view = name;
    VIEWS.forEach((v) => {
      $("#view-" + v).hidden = v !== name;
    });
    $$(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name)
    );
    if (name === "home") loadHome();
    else if (name === "approvals") loadApprovals();
    else if (name === "referrals") loadReferrals();
    else if (name === "inbox") loadInbox();
    else if (name === "chat") focusChat();
  }

  /* ================================================================
     AUTH
     ================================================================ */
  async function doLogin(ev) {
    ev.preventDefault();
    const btn = $("#login-submit");
    const errBox = $("#login-error");
    errBox.hidden = true;
    btn.disabled = true;
    btn.textContent = "Signing in…";
    try {
      const username = $("#login-username").value.trim();
      const password = $("#login-password").value;
      const me = await api("/api/login", {
        method: "POST",
        body: { username, password },
      });
      state.user = { username: me.username, role: me.role };
      renderWhoAmI();
      showApp();
      await checkHealth();
      setView("home");
      startPolling();
    } catch (err) {
      if (err instanceof Unauthorized) {
        errBox.textContent = "Invalid username or password.";
      } else {
        errBox.textContent = err.message || "Sign-in failed.";
      }
      errBox.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "Sign in";
    }
  }

  async function doLogout() {
    stopPolling();
    try { await api("/api/logout", { method: "POST" }); }
    catch { /* ignore; we're leaving anyway */ }
    state.user = null;
    state.chat = [];
    state.optimisticGone.clear();
    state.decidingIds.clear();
    $("#chat-log").innerHTML = "";  // clearing our own content only
    showLogin();
  }

  function renderWhoAmI() {
    if (!state.user) return;
    const role = state.user.role ? ` · ${titleCase(state.user.role)}` : "";
    $("#who-am-i").textContent = `${state.user.username}${role}`;
  }

  async function checkHealth() {
    const pill = $("#health-pill");
    try {
      const h = await api("/api/health");
      const parts = [];
      parts.push(h.emr_enabled ? "EMR on" : "EMR off");
      parts.push(`LLM: ${h.provider || "none"}`);
      if (h.provider && h.provider !== "none") {
        parts.push(h.phi_safe ? "PHI-safe" : "non-PHI-safe");
      }
      pill.classList.toggle("ok", !!h.ok);
      pill.classList.toggle("bad", !h.ok);
      $(".health-text", pill).textContent = parts.join(" · ");
    } catch (err) {
      if (err instanceof Unauthorized) return;
      pill.classList.add("bad");
      $(".health-text", pill).textContent = "offline";
    }
  }

  /* ================================================================
     HOME
     ================================================================ */
  async function loadHome(prefetched) {
    const body = $("#home-body");
    let home = prefetched || null;
    if (!home) {
      body.replaceChildren(txt("div", "loading", "Loading…"));
      try {
        home = await api("/api/home");
      } catch (err) {
        if (err instanceof Unauthorized) return;
        body.replaceChildren(errCard(err.message));
        return;
      }
    }

    const pending = Array.isArray(home.pending) ? home.pending : [];
    const legacy = Array.isArray(home.legacy) ? home.legacy : [];
    const referralsNew = Array.isArray(home.referrals_new)
      ? home.referrals_new : [];
    const inboxNew = Array.isArray(home.inbox_new) ? home.inbox_new : [];
    const report = home.manager_report || null;
    const audit = Array.isArray(home.audit) ? home.audit : [];

    // Nav badge counts the LIVE queue only (legacy is parked separately below),
    // so it reflects what still needs attention on the main dashboard.
    updateNavCounts(pending.length, referralsNew.length, inboxNew.length);

    // Right-rail summary reflects ALL pending awaiting approval (live + aged).
    renderSummaryPanel($("#home-summary"), pending);

    body.replaceChildren();

    // Left column: pending approvals — the LIVE queue (<=48h or unknown-age).
    // Aged (>48h) cards live in the Legacy Requests box below, not here.
    const left = el("div", "home-col");
    left.appendChild(sectionTitle("Pending approvals", pending.length));
    if (!pending.length) {
      left.appendChild(txt("div", "empty", "No approvals waiting."));
    } else {
      pending.forEach((card) =>
        left.appendChild(renderApprovalCard(card, { compact: true }))
      );
    }

    // Right column: new referrals
    const right = el("div", "home-col");
    right.appendChild(sectionTitle("New referrals", referralsNew.length));
    if (!referralsNew.length) {
      right.appendChild(txt("div", "empty", "No new referrals."));
    } else {
      referralsNew.forEach((r) =>
        right.appendChild(renderReferralCard(r, { compact: true }))
      );
    }

    body.appendChild(left);
    body.appendChild(right);

    // Wide: LEGACY requests — pending cards whose REAL source time is >48h old
    // (see request_age on the backend). Parked in its own collapsible box so
    // the aged backlog stays visible and actionable but never crowds the live
    // queue above. Rendered with the SAME renderApprovalCard as the live queue,
    // so approve/deny behave identically. Only shown when there is a backlog.
    if (legacy.length) {
      body.appendChild(renderLegacyBox(legacy));
    }

    // Wide: new inbox messages (untrusted external content — text only)
    const inboxWrap = el("div", "home-col home-wide");
    inboxWrap.appendChild(sectionTitle("Inbox — new", inboxNew.length));
    if (!inboxNew.length) {
      inboxWrap.appendChild(txt("div", "empty", "No new messages."));
    } else {
      const list = el("div", "inbox-list inbox-list-home");
      inboxNew.forEach((m) =>
        list.appendChild(renderInboxRow(m, { home: true }))
      );
      inboxWrap.appendChild(list);
    }
    body.appendChild(inboxWrap);

    // Wide: manager report
    const reportWrap = el("div", "home-col home-wide");
    reportWrap.appendChild(sectionTitle("Latest manager report"));
    reportWrap.appendChild(renderManagerReport(report));
    body.appendChild(reportWrap);

    // Wide: recent activity
    const auditWrap = el("div", "home-col home-wide");
    auditWrap.appendChild(sectionTitle("Recent activity", audit.length));
    auditWrap.appendChild(renderAudit(audit));
    body.appendChild(auditWrap);
  }

  function sectionTitle(text, count) {
    const h = el("div", "home-section-title");
    h.appendChild(document.createTextNode(text));
    if (typeof count === "number") {
      h.appendChild(txt("span", "count-chip", String(count)));
    }
    return h;
  }

  /**
   * Human "age" label from a card's real source time.
   * Uses the backend-resolved age_hours; falls back to "—" when the backend
   * could not establish a real source time (never fabricates an age). When the
   * age is based only on ingest time (not the true patient contact time) we say
   * so, so a fallback is never mistaken for a real patient timestamp.
   */
  function ageLabel(card) {
    const h = card && typeof card.age_hours === "number" ? card.age_hours : null;
    if (h === null) return DASH;
    let base;
    if (h >= 48) base = Math.floor(h / 24) + "d old";
    else if (h >= 1) base = Math.round(h) + "h old";
    else base = "under 1h old";
    // Flag when the age is only an ingest-time approximation.
    if (card.age_basis === "message_ingest_ts") base += " (ingest)";
    else if (card.age_basis === "card_created") base += " (created)";
    return base;
  }

  /**
   * Collapsible "Legacy Requests" box for the aged (>48h) pending backlog.
   * Kept visually and structurally separate from the live queue. Cards render
   * via the same renderApprovalCard, so approve/deny work identically; we only
   * prepend an honest age line. Count is the real backlog size — no fabrication.
   */
  function renderLegacyBox(legacy) {
    const details = el("details", "legacy-box home-wide");
    details.open = true; // visible by default; user can collapse to tuck away

    const summary = el("summary", "legacy-summary");
    summary.appendChild(txt("span", "legacy-title", "Legacy Requests"));
    summary.appendChild(txt("span", "count-chip", String(legacy.length)));
    summary.appendChild(
      txt("span", "legacy-hint", "older than 48h — real source time")
    );
    details.appendChild(summary);

    const list = el("div", "legacy-list");
    legacy.forEach((card) => {
      const item = el("div", "legacy-item");
      const age = el("div", "legacy-age");
      age.appendChild(txt("span", "legacy-age-badge", ageLabel(card)));
      if (card.age_source_ts) {
        age.appendChild(txt("span", "legacy-age-ts",
          "since " + fmtTs(card.age_source_ts)));
      }
      item.appendChild(age);
      item.appendChild(renderApprovalCard(card, { compact: true }));
      list.appendChild(item);
    });
    details.appendChild(list);
    return details;
  }

  function renderManagerReport(report) {
    const card = el("div", "card report-block");
    if (!report) {
      const p = txt("p", "muted");
      p.append("No manager report yet. Use ");
      p.appendChild(txt("b", null, "Run Manager"));
      p.append(" to generate one.");
      card.appendChild(p);
      return card;
    }
    // report may be {report:{...}, ts, window_hours} or the inner object.
    const inner = report.report && typeof report.report === "object"
      ? report.report : report;
    const summary = inner.summary || report.summary;
    const working = inner.working_well || [];
    const problems = inner.problems || [];
    const proposals = inner.proposals || [];
    const note = inner.note;

    card.appendChild(txt("p", "report-summary", val(summary)));
    const cols = el("div", "report-cols");
    cols.appendChild(reportList("Working well", working));
    cols.appendChild(reportList("Problems", problems));
    cols.appendChild(reportList("Proposals", proposals));
    card.appendChild(cols);

    const bits = [];
    if (report.ts) bits.push("as of " + fmtTs(report.ts));
    if (report.window_hours) bits.push(report.window_hours + "h window");
    if (note) bits.push(note);
    if (bits.length) {
      card.appendChild(txt("div", "report-meta", bits.join(" · ")));
    }
    return card;
  }

  function reportList(title, items) {
    const wrap = el("div", "report-list");
    wrap.appendChild(txt("h4", null, title));
    if (!items || !items.length) {
      wrap.appendChild(txt("p", "muted", DASH));
      return wrap;
    }
    const ul = el("ul");
    items.forEach((it) => {
      const text = typeof it === "string"
        ? it
        : (it && (it.text || it.title || it.detail)) || JSON.stringify(it);
      ul.appendChild(txt("li", null, text));
    });
    wrap.appendChild(ul);
    return wrap;
  }

  function renderAudit(rows) {
    const card = el("div", "card card-pad");
    if (!rows.length) {
      card.appendChild(txt("p", "muted", "No recent activity."));
      return card;
    }
    rows.forEach((r) => {
      const row = el("div", "audit-row");
      row.appendChild(txt("span", "audit-ts", fmtTs(r.ts)));
      row.appendChild(txt("span", "audit-kind", val(r.kind)));
      const action = val(r.action) + (r.detail ? " — " + val(r.detail) : "");
      row.appendChild(txt("span", "audit-action", action));
      const outcome = r.outcome || "ok";
      const cls = /err|fail|denied/i.test(outcome) ? "err" : "ok";
      row.appendChild(txt("span", "audit-outcome " + cls, outcome));
      card.appendChild(row);
    });
    return card;
  }

  function updateNavCounts(approvals, referrals, inbox) {
    const a = $("#nav-approvals-count");
    const r = $("#nav-referrals-count");
    const i = $("#nav-inbox-count");
    if (typeof approvals === "number") {
      a.textContent = approvals;
      a.hidden = approvals === 0;
    }
    if (typeof referrals === "number") {
      r.textContent = referrals;
      r.hidden = referrals === 0;
    }
    if (typeof inbox === "number") {
      i.textContent = inbox;
      i.hidden = inbox === 0;
    }
  }

  async function runManager(btn) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Running…";
    try {
      await api("/api/manager/run", { method: "POST" });
      toast("Manager report generated.", "ok");
      if (state.view === "home") await loadHome();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* ================================================================
     APPROVALS
     ================================================================ */
  async function loadApprovals() {
    const body = $("#approvals-body");
    const status = $("#approvals-filter").value;
    body.replaceChildren(txt("div", "loading", "Loading…"));
    let list;
    try {
      const qs = status ? `?status=${encodeURIComponent(status)}` : "";
      list = await api("/api/approvals" + qs);
    } catch (err) {
      if (err instanceof Unauthorized) return;
      body.replaceChildren(errCard(err.message));
      return;
    }
    let cards = Array.isArray(list) ? list : (list.approvals || []);
    // Hide cards we optimistically removed this session until the backend
    // confirms; on the "pending" filter this keeps a just-decided card gone.
    cards = cards.filter((c) => !state.optimisticGone.has(String(c.id)));

    // Keep the right-rail summary honest with the true pending set. When the
    // active filter is already "pending" we reuse these cards; otherwise we
    // fetch pending separately so the panel never reflects a filtered view.
    if (status === "pending") {
      renderSummaryPanel($("#approvals-summary"), cards);
    } else {
      refreshApprovalsSummary();
    }

    body.replaceChildren();
    if (!cards.length) {
      body.appendChild(txt("div", "empty", "No approvals for this filter."));
      return;
    }
    cards.forEach((c) => body.appendChild(renderApprovalCard(c)));
  }

  /**
   * Render one approval card.
   * card = { id, ts_created, ts_decided, status, action_type, params(obj),
   *          reason, requested_by, decided_by, decision_note, result(obj) }
   */
  function renderApprovalCard(card, opts = {}) {
    const status = (card.status || "pending").toLowerCase();
    const wrap = el("div", "approval status-" + status);
    if (card.id !== undefined) wrap.dataset.approvalId = String(card.id);

    // Head — type chip · status pill · #id (mono) · relative time
    const head = el("div", "approval-head");
    const left = el("div", "approval-head-left");
    left.appendChild(txt("span", "type-badge", titleCase(card.action_type)));
    left.appendChild(txt("span", "status-badge " + status, status));
    if (card.id !== undefined) {
      left.appendChild(txt("span", "approval-id", "#" + card.id));
    }
    head.appendChild(left);
    const when = txt("span", "card-sub", relTime(card.ts_created));
    when.title = fmtTs(card.ts_created);   // exact time on hover
    head.appendChild(when);
    wrap.appendChild(head);

    // Body
    const bd = el("div", "approval-body");

    // HERO — the human-readable reason/summary.
    const reason = el("div", "reason-box");
    reason.appendChild(txt("span", "reason-label", "Reason"));
    reason.appendChild(txt("span", "reason-text", val(card.reason)));
    bd.appendChild(reason);

    // Essential fields for this action type (tidy, scannable).
    const essential = renderEssential(card.action_type, card.params);
    if (essential) bd.appendChild(essential);

    // Node pathway — the REAL read/action/gate steps this card's automation
    // takes on approval (mirrors the app.approvals executor).
    bd.appendChild(renderPathway(card));

    // Raw params behind a subtle "Details" disclosure (progressive disclosure).
    bd.appendChild(renderParamsDisclosure(card.params));

    // Meta
    const meta = el("div", "approval-meta");
    const req = el("span");
    req.appendChild(txt("b", null, "Requested by"));
    req.append(" " + val(card.requested_by));
    meta.appendChild(req);
    if (card.decided_by) {
      const dec = el("span");
      dec.appendChild(txt("b", null, "Decided by"));
      dec.append(" " + val(card.decided_by) +
        (card.ts_decided ? " · " + fmtTs(card.ts_decided) : ""));
      meta.appendChild(dec);
    }
    bd.appendChild(meta);

    // Decision note (if already decided)
    if (card.decision_note) {
      bd.appendChild(
        txt("p", "decision-note-shown", "Note: " + card.decision_note)
      );
    }

    // Result / warning — verbatim from backend
    if (card.result && (status === "executed" || status === "failed"
        || card.result.error || card.result.warning)) {
      bd.appendChild(renderResult(card.result, status, card.action_type));
    }

    // Decision controls only while pending
    if (status === "pending") {
      bd.appendChild(renderDecisionBar(card));
    }

    wrap.appendChild(bd);
    return wrap;
  }

  /* ----------------------------------------------------------------
     NODE PATHWAY — the concrete read/action/gate steps the automation
     runs on approval. Kept faithful to the real executor in
     app/approvals.py (svigg_book/cancel/create_patient, comms.send_*,
     records-request flow) so it never overstates what the system does.
     kinds: read (look-up, no change) · action (a real write/send) ·
     gate (a human/authorization step) · verify (post-check).
     ---------------------------------------------------------------- */
  const KIND_LABEL = { read: "READ", action: "DO", gate: "GATE", verify: "CHECK" };

  function pathwayFor(card) {
    const p = card.params || {};
    const nm = [p.first_name, p.last_name].filter(Boolean).join(" ").trim()
      || p.patient_name || p.patient_phone || "the patient";
    const R = (label) => ({ kind: "read", label });
    const A = (label) => ({ kind: "action", label });
    const G = (label) => ({ kind: "gate", label });
    const V = (label) => ({ kind: "verify", label });
    const t = card.action_type;

    if (t === "book_appointment") {
      const when = [p.date_mdy, p.start_time].filter(Boolean).join(" ");
      return [
        R("Open Svigg"), R("Find " + nm + " in Patient View"),
        R("Open schedule · " + (p.date_mdy || "date")),
        A("Book " + (when || "slot") + (p.provider ? " · " + p.provider : "")),
        A("Force overbook if the slot is full"),
        V("Re-read calendar to confirm"),
      ];
    }
    if (t === "cancel_appointment") {
      return [
        R("Open Svigg"), R("Find " + nm),
        R("Locate the " + (p.date_iso || "appointment")),
        A("Cancel the appointment"), V("Confirm removal on the calendar"),
      ];
    }
    if (t === "reschedule_appointment") {
      return [
        R("Open Svigg"), A("Cancel the existing appointment"),
        V("Confirm the cancel"), A("Book the new slot"),
        V("Re-read calendar to confirm"),
      ];
    }
    if (t === "create_patient") {
      return [
        R("Open Svigg"), R("Search Patient View for " + nm + " (dedupe)"),
        R("Open the new-patient form"), A("Fill demographics"),
        G("SAVE held — dry-run until the contract is verified"),
      ];
    }
    if (t === "send_sms") {
      return [
        A("Compose the reply"),
        A("Send SMS via RingCentral" + (p.from_number ? " · from " + p.from_number : "")),
      ];
    }
    if (t === "send_email") {
      return [
        A("Compose the reply"), R("Fetch attachment(s) from Google Drive"),
        A("Send email via Gmail" + (p.to ? " · to " + p.to : "") + " — fail-closed if a file is missing"),
      ];
    }
    if (t === "manual_task") {
      const rt = p.request_type || "";
      if (rt === "records_request") {
        return [
          R("Identify " + nm), R("Search Google Drive for the patient's records"),
          R("Locate the requested document"),
          A("Draft email with patient info + attachment"),
          G("Human verifies authorization + approves before send"),
        ];
      }
      if (rt === "scheduling") {
        return [
          R("Identify " + nm), R("Check the Svigg calendar for availability"),
          G("Staff books via an approval card"),
        ];
      }
      if (rt === "billing") {
        return [
          R("Identify " + nm), R("Pull the billing ledger (SIS/Svigg)"),
          G("Staff reviews + responds"),
        ];
      }
      return [G("Manual review — no automated action wired for this yet")];
    }
    return [G("Manual review")];
  }

  function renderPathway(card) {
    const steps = pathwayFor(card);
    const box = el("div", "pathway");
    box.appendChild(txt("div", "pathway-title", "What the automation will do"));
    const chain = el("div", "pathway-chain");
    steps.forEach((s, i) => {
      const node = el("span", "pathway-node k-" + s.kind);
      node.appendChild(txt("span", "pathway-kind", KIND_LABEL[s.kind] || s.kind));
      node.appendChild(txt("span", "pathway-text", s.label));
      chain.appendChild(node);
      if (i < steps.length - 1) {
        const arr = txt("span", "pathway-arrow", "→");
        arr.setAttribute("aria-hidden", "true");
        chain.appendChild(arr);
      }
    });
    box.appendChild(chain);
    return box;
  }

  /** A scalar param that actually carries a value (skip empty/null). */
  function hasVal(v) {
    return v !== null && v !== undefined && v !== "" &&
      !(typeof v === "object");
  }

  /**
   * Tidy essential key-value block for a known action type. Renders only the
   * whitelisted fields (in order) that are present and scalar; long free-text
   * fields (body/message) get a full-width block. Returns null when there's
   * nothing essential to show, so the card falls back to the Details table.
   */
  function renderEssential(actionType, params) {
    if (!params || typeof params !== "object") return null;
    const fields = ESSENTIAL_FIELDS[actionType];
    if (!fields) return null;

    const grid = el("div", "essential");
    const seen = new Set();
    let rows = 0;
    fields.forEach((key) => {
      if (seen.has(key)) return;
      const v = params[key];
      if (!hasVal(v)) return;
      seen.add(key);
      rows++;
      grid.appendChild(txt("div", "essential-k", titleCase(key)));
      const vClass = MESSAGE_KEYS.has(key)
        ? "essential-v message"
        : (/^(to|from|from_number|phone|date|time|new_date|new_time|dob)$/.test(key)
            ? "essential-v mono" : "essential-v");
      grid.appendChild(txt("div", vClass, val(v)));
    });
    return rows ? grid : null;
  }

  /**
   * Raw params behind a collapsible "Details" disclosure. Pure-plumbing keys
   * (source_message_id, source_external_id, rowid, …) are filtered OUT of the
   * display entirely — they are internal wiring, not operator-facing data.
   * (Display-only: the backend still stores them unchanged.)
   */
  function renderParamsDisclosure(params) {
    const details = el("details", "params-details");
    const summary = el("summary", "params-summary");
    summary.textContent = "Details";
    details.appendChild(summary);
    details.appendChild(renderParams(params));
    return details;
  }

  function renderParams(params) {
    const table = el("table", "params-table");
    const entries = (params && typeof params === "object")
      ? Object.entries(params).filter(([k]) => !PLUMBING_KEYS.has(k))
      : [];
    if (!entries.length) {
      const tr = el("tr");
      const td = txt("td", "muted", "No parameters");
      td.colSpan = 2;
      tr.appendChild(td);
      table.appendChild(tr);
      return table;
    }
    for (const [k, v] of entries) {
      const tr = el("tr");
      tr.appendChild(txt("th", null, titleCase(k)));
      const td = el("td");
      if (v && typeof v === "object") {
        td.appendChild(renderNestedParams(v));   // nested (reschedule)
      } else {
        td.textContent = val(v);
      }
      tr.appendChild(td);
      table.appendChild(tr);
    }
    return table;
  }

  function renderNestedParams(obj) {
    const box = el("div", "params-nested");
    const t = el("table", "params-table");
    for (const [k, v] of Object.entries(obj)) {
      if (PLUMBING_KEYS.has(k)) continue;
      const tr = el("tr");
      tr.appendChild(txt("th", null, titleCase(k)));
      tr.appendChild(txt("td", null,
        (v && typeof v === "object") ? JSON.stringify(v) : val(v)));
      t.appendChild(tr);
    }
    box.appendChild(t);
    return box;
  }

  /**
   * Result box. Shows the EMR's own text VERBATIM — we never synthesize
   * a "success" string. Warnings surface in their own highlighted line.
   */
  function renderResult(result, status, actionType) {
    const ok = status === "executed";
    const box = el("div", "result-box " + (ok ? "result-ok" : "result-bad"));
    box.appendChild(txt("div", "result-title",
      ok ? "Execution result" : "Result — not executed"));

    if (typeof result === "string") {
      box.appendChild(txt("p", "result-line", result));
      return box;
    }

    // Known verbatim fields, shown as-is via textContent.
    const showField = (key, label) => {
      if (result[key] !== undefined && result[key] !== null
          && result[key] !== "") {
        const p = el("p", "result-line");
        p.appendChild(txt("b", null, label + ":"));
        p.append(" " + String(result[key]));
        box.appendChild(p);
      }
    };
    showField("emr_status", "EMR status");
    showField("status", "Status");
    showField("verified", "Verified");
    showField("error", "Error");

    // Warning — the EMR's own text, verbatim, highlighted.
    if (result.warning) {
      const w = el("div", "warning-line");
      w.appendChild(txt("b", null, "Warning:"));
      w.append(" " + String(result.warning));
      box.appendChild(w);
    }
    // Booking/reschedule has no auto-verify — surface caveat (contract rule).
    // Gated to EMR-write actions: an SMS/email send carries its own result
    // ("sms sent") and must NOT show a spurious "confirm in the EMR" note.
    const isBooking = actionType === "book_appointment"
      || actionType === "reschedule_appointment";
    if (ok && isBooking && result.verified === undefined && !result.warning) {
      const c = el("div", "warning-line");
      c.appendChild(txt("b", null, "Note:"));
      c.append(" booking has no auto-verify — confirm in the EMR.");
      box.appendChild(c);
    }

    // Full raw EMR response, verbatim, for auditability.
    box.appendChild(txt("pre", "result-raw", safeStringify(result)));
    return box;
  }

  function safeStringify(obj) {
    try { return JSON.stringify(obj, null, 2); }
    catch { return String(obj); }
  }

  function renderDecisionBar(card) {
    const bar = el("div", "decision-bar");

    const noteWrap = el("label", "decision-note");
    noteWrap.appendChild(txt("span", null, "Note (optional)"));
    const note = el("input");
    note.type = "text";
    note.placeholder = "Add a note for the record…";
    noteWrap.appendChild(note);
    bar.appendChild(noteWrap);

    const check = txt("button", "btn btn-ghost btn-check", "Still needed?");
    const approve = txt("button", "btn btn-approve", "Approve");
    const deny = txt("button", "btn btn-deny", "Deny");

    const verdict = el("div", "verify-verdict");
    verdict.hidden = true;

    check.addEventListener("click", () => verifyNeed(card, check, verdict));
    approve.addEventListener("click", () =>
      decide(card, true, note.value, [approve, deny]));
    deny.addEventListener("click", () =>
      decide(card, false, note.value, [approve, deny]));

    bar.appendChild(check);
    bar.appendChild(deny);
    bar.appendChild(approve);

    const wrap = el("div", "decision-wrap");
    wrap.appendChild(bar);
    wrap.appendChild(verdict);
    return wrap;
  }

  const VERDICT_LABEL = {
    likely_resolved: "Likely handled", still_open: "Still open",
    exists_manual: "Review manually", no_patient: "No patient found",
    error: "Couldn't check",
  };

  /** On-demand: ask the backend to cross-reference this card vs live EMR. */
  async function verifyNeed(card, btn, slot) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Checking EMR…";
    try {
      const r = await api(
        `/api/approvals/${encodeURIComponent(card.id)}/verify-need`,
        { method: "POST" }
      );
      slot.className = "verify-verdict v-" + (r.verdict || "unknown");
      slot.replaceChildren(
        txt("span", "verify-tag", VERDICT_LABEL[r.verdict] || "Checked"),
        txt("span", "verify-detail", r.detail || "")
      );
      slot.hidden = false;
    } catch (err) {
      if (err instanceof Unauthorized) return;
      slot.className = "verify-verdict v-error";
      slot.replaceChildren(txt("span", "verify-detail", err.message));
      slot.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function decide(card, approve, note, buttons) {
    buttons.forEach((b) => (b.disabled = true));
    const id = String(card.id);
    // Mark in-flight so the poll loop leaves this card (and its note) alone.
    state.decidingIds.add(id);
    try {
      const updated = await api(
        `/api/approvals/${encodeURIComponent(card.id)}/decision`,
        { method: "POST", body: { approve, note: note || "" } }
      );
      const newCard = (updated && updated.id !== undefined)
        ? updated
        : (updated && updated.card) ||
          { ...card, status: approve ? "approved" : "denied" };
      const st = (newCard.status || "").toLowerCase();

      // The card is no longer pending. Optimistically drop it from every
      // pending list (this view + other tabs converge on their next poll).
      // This is honest: the backend just told us it's decided.
      state.optimisticGone.add(id);

      const old = buttons[0].closest(".approval");
      const onApprovals = state.view === "approvals";
      const filter = onApprovals ? $("#approvals-filter").value : null;
      // On Approvals with a filter that still includes this card (e.g. "All",
      // or the exact new status), show the decided result in place so the
      // operator sees the EMR outcome. Otherwise remove the card from view.
      const keepVisible = onApprovals &&
        (filter === "" || filter === st);
      if (keepVisible) {
        const fresh = renderApprovalCard(newCard);
        if (old && old.parentNode) old.replaceWith(fresh);
      } else if (old && old.parentNode) {
        old.remove();
        // If the list is now empty, show the empty state.
        const bodyEl = onApprovals ? $("#approvals-body") : null;
        if (bodyEl && !bodyEl.querySelector(".approval")) {
          bodyEl.replaceChildren(
            txt("div", "empty", "No approvals for this filter."));
        }
      }

      if (st === "executed") toast("Approved — EMR reported executed.", "ok");
      else if (st === "failed") toast("Approved, but EMR did not confirm.", "bad");
      else if (st === "denied") toast("Request denied.", "");
      else toast("Decision recorded.", "");

      // Update the right-rail panel + nav counts immediately.
      if (state.view === "approvals") refreshApprovalsSummary();
      refreshCounts();
      // Decision settled: release the poll guard. optimisticGone still hides
      // the card until the backend confirms it's no longer pending, so the
      // list stays correct across the next poll.
      state.decidingIds.delete(id);
    } catch (err) {
      state.decidingIds.delete(id);
      if (!(err instanceof Unauthorized)) {
        toast(err.message, "bad");
        buttons.forEach((b) => (b.disabled = false));
      }
    }
  }

  /* ================================================================
     AWAITING-APPROVAL SUMMARY PANEL  (right rail: Home + Approvals)

     Honest by construction: every number here is derived from the SAME
     pending card list the main column shows — a real count, a real
     breakdown by action_type, and the real next-few cards. Optimistically
     removed cards (state.optimisticGone) are filtered out so a just-decided
     card leaves the panel immediately, then real state reconciles on poll.
     ================================================================ */

  /** True pending cards only, minus anything optimistically removed. */
  function livePending(cards) {
    return (Array.isArray(cards) ? cards : []).filter((c) =>
      (c.status || "pending").toLowerCase() === "pending" &&
      !state.optimisticGone.has(String(c.id))
    );
  }

  function renderSummaryPanel(mount, pending) {
    if (!mount) return;
    const cards = livePending(pending);

    const panel = el("div", "summary-card");

    // Head — title + true total.
    const head = el("div", "summary-head");
    head.appendChild(txt("span", "summary-title", "Awaiting approval"));
    const totalWrap = el("span");
    totalWrap.style.marginLeft = "auto";
    totalWrap.style.textAlign = "right";
    totalWrap.appendChild(txt("div", "summary-total", String(cards.length)));
    totalWrap.appendChild(txt("div", "summary-total-label", "pending"));
    head.appendChild(totalWrap);
    panel.appendChild(head);

    if (!cards.length) {
      panel.appendChild(txt("div", "summary-empty", "Nothing waiting."));
      mount.replaceChildren(panel);
      return;
    }

    // Breakdown by action_type — real counts.
    const counts = new Map();
    cards.forEach((c) => {
      const t = c.action_type || "other";
      counts.set(t, (counts.get(t) || 0) + 1);
    });
    const brk = el("div", "summary-breakdown");
    Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .forEach(([type, n]) => {
        const row = el("div", "summary-brk-row");
        row.appendChild(txt("span", "type-badge", titleCase(type)));
        row.appendChild(txt("span", "summary-brk-count", String(n)));
        brk.appendChild(row);
      });
    panel.appendChild(brk);

    // Next few pending cards — click scrolls to the card in the main column.
    panel.appendChild(txt("div", "summary-next-title", "Next up"));
    const list = el("div", "summary-list");
    cards.slice(0, 5).forEach((c) => {
      const item = el("button", "summary-item");
      item.type = "button";
      const idLabel = c.id !== undefined ? "#" + c.id : "new";
      item.setAttribute("aria-label",
        `Go to ${titleCase(c.action_type)} ${idLabel}`);

      const top = el("div", "summary-item-top");
      top.appendChild(txt("span", "type-badge", titleCase(c.action_type)));
      top.appendChild(txt("span", "summary-item-id", idLabel));
      item.appendChild(top);

      const titleLine = c.reason || titleCase(c.action_type);
      item.appendChild(txt("div", "summary-item-title", titleLine));

      const meta = el("div", "summary-item-meta");
      meta.appendChild(txt("span", "summary-item-id", idLabel));
      meta.appendChild(document.createTextNode("·"));
      meta.appendChild(txt("span", null, relTime(c.ts_created)));
      item.appendChild(meta);

      item.addEventListener("click", () => focusApprovalCard(c.id));
      list.appendChild(item);
    });
    panel.appendChild(list);

    mount.replaceChildren(panel);
  }

  /** Scroll to and briefly highlight an approval card by id (current view). */
  function focusApprovalCard(id) {
    const sel = `.approval[data-approval-id="${CSS.escape(String(id))}"]`;
    const node = document.querySelector(sel);
    if (!node) {
      // Not on screen (e.g. filtered out on Approvals) — jump to Approvals.
      if (state.view !== "approvals") setView("approvals");
      return;
    }
    node.scrollIntoView({ behavior: "smooth", block: "center" });
    node.classList.add("flash-target");
    setTimeout(() => node.classList.remove("flash-target"), 1400);
  }

  /** Refresh only the Approvals summary rail from real pending state. */
  async function refreshApprovalsSummary() {
    const mount = $("#approvals-summary");
    if (!mount) return;
    try {
      const list = await api("/api/approvals?status=pending");
      const cards = Array.isArray(list) ? list : (list.approvals || []);
      renderSummaryPanel(mount, cards);
    } catch { /* non-fatal; leave last-rendered panel */ }
  }

  /* ================================================================
     REFERRALS
     ================================================================ */
  async function loadReferrals() {
    const body = $("#referrals-body");
    const status = $("#referrals-filter").value;
    body.replaceChildren(txt("div", "loading", "Loading…"));
    let list;
    try {
      const qs = status ? `?status=${encodeURIComponent(status)}` : "";
      list = await api("/api/referrals" + qs);
    } catch (err) {
      if (err instanceof Unauthorized) return;
      body.replaceChildren(errCard(err.message));
      return;
    }
    const refs = Array.isArray(list) ? list : (list.referrals || []);
    body.replaceChildren();
    if (!refs.length) {
      body.appendChild(txt("div", "empty", "No referrals for this filter."));
      return;
    }
    refs.forEach((r) => body.appendChild(renderReferralCard(r)));
  }

  /**
   * referral = { id, ts, source, patient_name, dob, phone, referrer,
   *              reason, status, detail, candidates }
   */
  function renderReferralCard(ref, opts = {}) {
    const status = (ref.status || "new").toLowerCase();
    const wrap = el("div", "referral");

    const head = el("div", "referral-head");
    head.appendChild(txt("span", "referral-name", val(ref.patient_name)));
    head.appendChild(txt("span", "status-badge " + status, status));
    wrap.appendChild(head);

    const grid = el("div", "referral-grid");
    grid.appendChild(field("DOB", ref.dob));
    grid.appendChild(field("Phone", ref.phone));
    grid.appendChild(field("Referrer", ref.referrer));
    grid.appendChild(field("Source", ref.source));
    grid.appendChild(field("Received", fmtTs(ref.ts)));
    wrap.appendChild(grid);

    if (ref.reason) {
      const r = el("div", "referral-field");
      r.appendChild(txt("span", "k", "Reason"));
      r.appendChild(txt("span", "v", val(ref.reason)));
      wrap.appendChild(r);
    }

    if (ref.detail) {
      const detailText = typeof ref.detail === "object"
        ? safeStringify(ref.detail) : String(ref.detail);
      wrap.appendChild(txt("div", "referral-detail", detailText));
    }

    // Candidate matches from combined_search (may carry an error object).
    if (ref.candidates) {
      wrap.appendChild(renderCandidates(ref.candidates));
    }

    if (!opts.compact) {
      wrap.appendChild(renderReferralActions(ref));
    }
    return wrap;
  }

  function field(k, v) {
    const f = el("div", "referral-field");
    f.appendChild(txt("span", "k", k));
    f.appendChild(txt("span", "v", val(v)));
    return f;
  }

  function renderCandidates(candidates) {
    let data = candidates;
    if (typeof candidates === "string") {
      try { data = JSON.parse(candidates); } catch { data = candidates; }
    }
    const box = el("div", "referral-detail");
    if (data && data.error) {
      box.appendChild(txt("span", "k", "EMR match"));
      box.append(" ");
      box.appendChild(txt("span", "muted", "unavailable — " + data.error));
      return box;
    }
    const matches = Array.isArray(data)
      ? data
      : (data && Array.isArray(data.results) ? data.results : []);
    if (!matches.length) {
      box.appendChild(txt("span", "k", "EMR match"));
      box.append(" ");
      box.appendChild(txt("span", "muted", DASH));
      return box;
    }
    box.appendChild(txt("span", "k", `EMR matches (${matches.length})`));
    const ul = el("ul");
    ul.style.margin = "4px 0 0";
    ul.style.paddingLeft = "16px";
    matches.slice(0, 5).forEach((m) => {
      const label = m.name || m.patient_name || m.display ||
        [m.last_name, m.first_name].filter(Boolean).join(", ") ||
        JSON.stringify(m);
      ul.appendChild(txt("li", null, label));
    });
    box.appendChild(ul);
    return box;
  }

  function renderReferralActions(ref) {
    const bar = el("div", "referral-actions");

    const sel = el("label", "filter");
    sel.appendChild(txt("span", null, "Set status"));
    const select = el("select");
    ["new", "working", "booked", "declined"].forEach((s) => {
      const o = txt("option", null, titleCase(s));
      o.value = s;
      if (s === (ref.status || "new")) o.selected = true;
      select.appendChild(o);
    });
    sel.appendChild(select);
    bar.appendChild(sel);

    const noteWrap = el("label", "decision-note");
    noteWrap.appendChild(txt("span", null, "Note (optional)"));
    const note = el("input");
    note.type = "text";
    note.placeholder = "Add a note…";
    noteWrap.appendChild(note);
    bar.appendChild(noteWrap);

    const save = txt("button", "btn btn-primary btn-sm", "Update");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await api(`/api/referrals/${encodeURIComponent(ref.id)}/update`, {
          method: "POST",
          body: { status: select.value, note: note.value || "" },
        });
        toast("Referral updated.", "ok");
        await loadReferrals();
        refreshCounts();
      } catch (err) {
        if (!(err instanceof Unauthorized)) toast(err.message, "bad");
        save.disabled = false;
      }
    });
    bar.appendChild(save);
    return bar;
  }

  async function ingestReferrals(btn) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Ingesting…";
    try {
      const r = await api("/api/referrals/ingest", { method: "POST" });
      const drop = r && r.drop !== undefined ? r.drop : 0;
      const sheet = r && r.sheet !== undefined ? r.sheet : "—";
      toast(`Ingested: ${drop} from drop folder · sheet: ${sheet}`, "ok");
      await loadReferrals();
      refreshCounts();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  async function runReferralAudit(btn) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Scanning… (this can take a minute)";
    try {
      const r = await api("/api/referrals/audit", {
        method: "POST",
        body: { scan_gmail: true, window_days: 21 },
      });
      if (r && r.status === "gmail_not_configured") {
        toast("Referral audit: Gmail is not configured.", "bad");
      } else {
        const checked = r && r.checked !== undefined ? r.checked : 0;
        const created = r && r.cards_created !== undefined ? r.cards_created : 0;
        const scheduled = r && r.scheduled !== undefined ? r.scheduled : 0;
        toast(
          "Referral audit: " + checked + " checked · " + created +
            " card(s) created · " + scheduled + " already scheduled.",
          "ok"
        );
      }
      await loadReferrals();
      refreshCounts();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* ================================================================
     INBOX

     Messages are UNTRUSTED external content (email/SMS from anyone):
     senders, subjects, and bodies are placed with textContent ONLY —
     never innerHTML. Same posture as the rest of the app.
     ================================================================ */
  async function loadInbox() {
    const list = $("#inbox-list");
    const channel = $("#inbox-channel-filter").value;
    const status = $("#inbox-status-filter").value;
    list.replaceChildren(txt("div", "loading", "Loading…"));
    let rows;
    try {
      const qs = [];
      if (channel) qs.push("channel=" + encodeURIComponent(channel));
      if (status) qs.push("status=" + encodeURIComponent(status));
      const q = qs.length ? "?" + qs.join("&") : "";
      const data = await api("/api/messages" + q);
      rows = Array.isArray(data) ? data : (data.messages || []);
    } catch (err) {
      if (err instanceof Unauthorized) return;
      list.replaceChildren(errCard(err.message));
      return;
    }

    state.inboxRows = rows;
    list.replaceChildren();
    if (!rows.length) {
      list.appendChild(txt("div", "empty", "No messages for this filter."));
      state.inboxSelected = null;
      resetInboxDetail();
      return;
    }
    rows.forEach((m) => list.appendChild(renderInboxRow(m)));

    // Re-open the previously selected message if it's still in the list;
    // otherwise leave the placeholder. The list rows carry the FULL body,
    // so the detail pane reads from cache (no per-message GET endpoint).
    if (state.inboxSelected !== null &&
        rows.some((m) => String(m.id) === String(state.inboxSelected))) {
      showInboxDetail(state.inboxSelected);
    } else {
      state.inboxSelected = null;
      resetInboxDetail();
    }
  }

  /** Find a cached row by id (rows carry the full body). */
  function inboxRowById(id) {
    return state.inboxRows.find((m) => String(m.id) === String(id)) || null;
  }

  /**
   * message = { id, ts, channel, direction, sender, recipient, subject,
   *             body, external_id, thread_ref, status, detail }
   * Row: channel badge, sender, subject-or-body preview, time, status.
   * opts.home -> compact row used on the Home dashboard (no selection).
   */
  function renderInboxRow(m, opts = {}) {
    const status = (m.status || "new").toLowerCase();
    const channel = (m.channel || "email").toLowerCase();
    const row = el("button", "inbox-row status-" + status);
    row.type = "button";
    if (m.id !== undefined) row.dataset.id = String(m.id);
    if (!opts.home && m.id !== undefined &&
        String(m.id) === String(state.inboxSelected)) {
      row.classList.add("selected");
    }

    const top = el("div", "inbox-row-top");
    top.appendChild(txt("span", "channel-badge " + channel, channel));
    if (m.direction) {
      top.appendChild(txt("span", "dir-badge " + (m.direction === "out"
        ? "dir-out" : "dir-in"),
        m.direction === "out" ? "out" : "in"));
    }
    // Inbound shows sender; outbound shows recipient.
    const who = m.direction === "out" ? m.recipient : m.sender;
    top.appendChild(txt("span", "inbox-sender", val(who)));
    top.appendChild(txt("span", "inbox-time", fmtTs(m.ts)));
    row.appendChild(top);

    const preview = m.subject || m.body || "";
    row.appendChild(txt("div", "inbox-preview", val(preview)));

    const foot = el("div", "inbox-row-foot");
    foot.appendChild(txt("span", "status-badge " + status, status));
    row.appendChild(foot);

    if (!opts.home) {
      row.addEventListener("click", () => showInboxDetail(m.id));
    } else {
      // From Home: remember the selection, jump into the Inbox view.
      // loadInbox() re-opens the remembered message once rows load.
      row.addEventListener("click", () => {
        state.inboxSelected = m.id;
        setView("inbox");
      });
    }
    return row;
  }

  function resetInboxDetail() {
    const pane = $("#inbox-detail");
    pane.replaceChildren(
      txt("div", "empty", "Select a message to read it here.")
    );
  }

  /**
   * Open a message in the detail pane. Reads from the cached list row
   * (rows already carry the full body) — the contract defines no
   * single-message GET endpoint, so we never fetch one.
   */
  function showInboxDetail(id) {
    state.inboxSelected = id;
    // Highlight the active row.
    $$("#inbox-list .inbox-row").forEach((r) =>
      r.classList.toggle("selected", r.dataset.id === String(id))
    );
    const pane = $("#inbox-detail");
    const msg = inboxRowById(id);
    if (!msg) {
      pane.replaceChildren(txt("div", "empty", "Message not found."));
      return;
    }
    pane.replaceChildren(renderInboxDetail(msg));
  }

  /** Full message detail pane — FULL body via textContent (untrusted). */
  function renderInboxDetail(m) {
    const status = (m.status || "new").toLowerCase();
    const channel = (m.channel || "email").toLowerCase();
    const wrap = el("div", "inbox-detail-card");

    // Header
    const head = el("div", "inbox-detail-head");
    const badges = el("div", "inbox-detail-badges");
    badges.appendChild(txt("span", "channel-badge " + channel, channel));
    if (m.direction) {
      badges.appendChild(txt("span", "dir-badge " + (m.direction === "out"
        ? "dir-out" : "dir-in"),
        m.direction === "out" ? "outbound" : "inbound"));
    }
    badges.appendChild(txt("span", "status-badge " + status, status));
    if (m.id !== undefined) {
      badges.appendChild(txt("span", "approval-id", "#" + m.id));
    }
    head.appendChild(badges);
    head.appendChild(txt("span", "card-sub", fmtTs(m.ts)));
    wrap.appendChild(head);

    // Subject
    wrap.appendChild(txt("h2", "inbox-detail-subject", val(m.subject)));

    // Meta grid (from / to / thread)
    const grid = el("div", "inbox-detail-meta");
    grid.appendChild(field("From", m.sender));
    grid.appendChild(field("To", m.recipient));
    if (m.thread_ref) grid.appendChild(field("Thread", m.thread_ref));
    wrap.appendChild(grid);

    // FULL body — textContent (whitespace preserved via CSS).
    const bodyBox = el("div", "inbox-detail-body");
    bodyBox.textContent = m.body === null || m.body === undefined ||
      m.body === "" ? DASH : String(m.body);
    wrap.appendChild(bodyBox);

    // detail (raw HTML-tag note etc.) — surfaced verbatim, text only.
    if (m.detail) {
      const detailText = typeof m.detail === "object"
        ? safeStringify(m.detail) : String(m.detail);
      wrap.appendChild(txt("div", "inbox-detail-note", detailText));
    }

    // Actions
    wrap.appendChild(renderInboxActions(m));
    return wrap;
  }

  function renderInboxActions(m) {
    const bar = el("div", "inbox-actions");

    // Draft reply in chat — only meaningful for inbound messages.
    if (m.direction !== "out") {
      const draft = txt("button", "btn btn-primary btn-sm",
        "Draft reply in chat");
      draft.addEventListener("click", () => draftReplyInChat(m));
      bar.appendChild(draft);
    }

    // Status buttons — Triaged / Replied / Archived.
    [["triaged", "Triaged"], ["replied", "Replied"], ["archived", "Archived"]]
      .forEach(([st, label]) => {
        const b = txt("button", "btn btn-ghost btn-sm", label);
        if ((m.status || "").toLowerCase() === st) b.disabled = true;
        b.addEventListener("click", () => updateInboxStatus(m, st, bar));
        bar.appendChild(b);
      });

    return bar;
  }

  /**
   * Prefill the chat input (do NOT auto-send). The reply body will be
   * composed by the assistant and queued to on-screen approval.
   *
   * SECURITY: reference the message by id ONLY. The sender/subject/body of an
   * inbound email are attacker-controlled UNTRUSTED content; interpolating them
   * here would launder them into the chat input, which becomes a trusted
   * user-role instruction on send. The agent fetches the untrusted content
   * itself via message_detail, where the untrusted-content rule applies.
   */
  function draftReplyInChat(m) {
    const prompt = `Draft a reply to inbox message #${m.id}`;
    setView("chat");
    const inp = $("#chat-input");
    if (inp) {
      inp.value = prompt;
      autoGrow(inp);
      setTimeout(() => {
        inp.focus();
        inp.setSelectionRange(inp.value.length, inp.value.length);
      }, 60);
    }
  }

  async function updateInboxStatus(m, status, bar) {
    const buttons = $$("button", bar);
    buttons.forEach((b) => (b.disabled = true));
    try {
      await api("/api/messages/" + encodeURIComponent(m.id) + "/update", {
        method: "POST",
        body: { status },
      });
      toast("Message marked " + status + ".", "ok");
      // Reload the list; loadInbox() re-opens the still-selected message
      // (state.inboxSelected) and re-renders its detail with fresh data.
      m.status = status;
      await loadInbox();
      refreshCounts();
    } catch (err) {
      if (!(err instanceof Unauthorized)) {
        toast(err.message, "bad");
        buttons.forEach((b) => (b.disabled = false));
      }
    }
  }

  async function pollInbox(btn) {
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Polling…";
    try {
      const r = await api("/api/messages/poll", { method: "POST" });
      const fmt = (v) => (typeof v === "number" ? v + " new" : String(v));
      const email = r && r.email !== undefined ? fmt(r.email) : "—";
      const sms = r && r.sms !== undefined ? fmt(r.sms) : "—";
      // Per-account email detail (multi-mailbox). Surface only what the
      // backend actually returned — a per-account error must stay visible.
      let acctNote = "";
      const accts = r && r.email_accounts;
      if (accts && typeof accts === "object") {
        const parts = Object.keys(accts).map((a) => `${a}: ${fmt(accts[a])}`);
        if (parts.length) acctNote = ` (${parts.join(" · ")})`;
      }
      toast(`Polled · email: ${email}${acctNote} · sms: ${sms}`, "ok");
      await loadInbox();
      refreshCounts();
    } catch (err) {
      if (!(err instanceof Unauthorized)) toast(err.message, "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* ================================================================
     CHAT
     ================================================================ */
  function focusChat() {
    const inp = $("#chat-input");
    if (inp) setTimeout(() => inp.focus(), 40);
  }

  function appendChatMessage(role, content) {
    const log = $("#chat-log");
    const msg = el("div", "msg " + role);
    msg.appendChild(txt("div", "msg-role", role === "user" ? "You" : "Assistant"));
    msg.appendChild(txt("div", "bubble", content));
    log.appendChild(msg);
    log.scrollTop = log.scrollHeight;
    return msg;
  }

  function appendTyping() {
    const log = $("#chat-log");
    const msg = el("div", "msg assistant");
    msg.id = "chat-typing";
    msg.appendChild(txt("div", "msg-role", "Assistant"));
    const b = el("div", "bubble");
    const typing = el("div", "typing");
    typing.appendChild(el("span"));
    typing.appendChild(el("span"));
    typing.appendChild(el("span"));
    b.appendChild(typing);
    msg.appendChild(b);
    log.appendChild(msg);
    log.scrollTop = log.scrollHeight;
  }

  function removeTyping() {
    const t = $("#chat-typing");
    if (t) t.remove();
  }

  function appendToolChips(events) {
    if (!events || !events.length) return;
    const log = $("#chat-log");
    const wrap = el("div", "tool-chips");
    events.forEach((e) => {
      const chip = el("div", "tool-chip" + (e.ok ? "" : " bad"));
      chip.appendChild(el("span", "tdot"));
      chip.appendChild(txt("span", "tname", e.tool || "tool"));
      if (e.summary) {
        chip.append(" " + val(e.summary));
      }
      wrap.appendChild(chip);
    });
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }

  function appendInlineApprovals(cards) {
    if (!cards || !cards.length) return;
    const log = $("#chat-log");
    cards.forEach((c) => {
      const box = el("div", "inline-approval");
      box.append("Queued for on-screen approval ");
      box.appendChild(txt("span", "ia-id",
        c.id !== undefined ? "#" + c.id : "new"));
      box.append(" · " + titleCase(c.action_type) + " · ");
      const link = txt("button", "link-btn", "Review in Approvals");
      link.addEventListener("click", () => setView("approvals"));
      box.appendChild(link);
      log.appendChild(box);
    });
    log.scrollTop = log.scrollHeight;
  }

  async function sendChat(ev) {
    if (ev) ev.preventDefault();
    if (state.chatBusy) return;
    const input = $("#chat-input");
    const message = input.value.trim();
    if (!message) return;

    input.value = "";
    autoGrow(input);
    appendChatMessage("user", message);
    state.chat.push({ role: "user", content: message });

    state.chatBusy = true;
    $("#chat-send").disabled = true;
    appendTyping();

    try {
      const res = await api("/api/chat", {
        method: "POST",
        body: { message },
      });
      removeTyping();
      const reply = (res && res.reply) || "(no reply)";
      appendChatMessage("assistant", reply);
      state.chat.push({ role: "assistant", content: reply });
      appendToolChips(res && res.tool_events);
      appendInlineApprovals(res && res.approvals_created);
      if (res && res.approvals_created && res.approvals_created.length) {
        refreshCounts();
      }
    } catch (err) {
      removeTyping();
      if (!(err instanceof Unauthorized)) {
        appendChatMessage("assistant", "Error: " + err.message);
      }
    } finally {
      state.chatBusy = false;
      $("#chat-send").disabled = false;
      focusChat();
    }
  }

  function autoGrow(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
  }

  /* ================================================================
     SHARED
     ================================================================ */
  function errCard(message) {
    const c = txt("div", "empty", message || "Something went wrong.");
    c.style.borderColor = "var(--danger-soft)";
    c.style.color = "var(--danger)";
    return c;
  }

  /** Refresh sidebar counts silently from /api/home. */
  async function refreshCounts() {
    try {
      const home = await api("/api/home");
      updateNavCounts(
        Array.isArray(home.pending) ? home.pending.length : 0,
        Array.isArray(home.referrals_new) ? home.referrals_new.length : 0,
        Array.isArray(home.inbox_new) ? home.inbox_new.length : 0
      );
    } catch { /* non-fatal */ }
  }

  /* ================================================================
     LIVE CROSS-VIEW SYNC  (lightweight ~6s poll)

     Re-fetches the current view's data, the nav counts, and the summary
     panel, then re-renders WITHOUT destroying user state:
       - window scroll position is preserved
       - open <details> disclosures stay open
       - if the user is typing in a field (a decision note, the chat box,
         a filter), the list re-render is skipped that cycle so focus and
         in-progress text are never lost (counts + panel still update)
       - a card with a decision in flight is never re-rendered
     Pauses while the tab is hidden; resumes on focus/visibility.

     Honesty: the poll only ever reflects real backend state. A card the
     backend reports as decided disappears from every pending list; nothing
     is invented or kept stale.
     ================================================================ */
  const POLL_MS = 6000;

  /** True when the user is actively typing somewhere in the app content. */
  function isEditingText() {
    const a = document.activeElement;
    if (!a) return false;
    const tag = a.tagName;
    if (tag === "TEXTAREA") return true;
    if (tag === "INPUT") {
      const t = (a.getAttribute("type") || "text").toLowerCase();
      return ["text", "search", "email", "tel", "password", "number", "url"]
        .includes(t);
    }
    return false;
  }

  /** Set of ids for currently-open Details disclosures (to restore them). */
  function openDetailIds(root) {
    const open = new Set();
    $$(".approval[data-approval-id]", root).forEach((card) => {
      const d = card.querySelector("details.params-details");
      if (d && d.open) open.add(card.dataset.approvalId);
    });
    return open;
  }
  function restoreOpenDetails(root, ids) {
    if (!ids || !ids.size) return;
    $$(".approval[data-approval-id]", root).forEach((card) => {
      if (ids.has(card.dataset.approvalId)) {
        const d = card.querySelector("details.params-details");
        if (d) d.open = true;
      }
    });
  }

  async function pollTick() {
    if (document.hidden || state.polling) return;
    if (!state.user) return;
    if (state.view !== "home" && state.view !== "approvals") {
      // Other views: keep the nav badges live, cheaply.
      refreshCounts();
      return;
    }
    state.polling = true;
    // Rebuild optimisticGone each cycle from confirmed state so it can't
    // permanently hide a card — it's only a bridge until the next real fetch.
    // Treat an IN-FLIGHT decision like active editing: while a card's approve/
    // deny is awaiting the backend (EMR write, up to tens of seconds), a poll
    // must NOT replaceChildren the list — that would detach the card node
    // decide() holds, wiping the typed note and the decided-result render
    // (audit #11/#12). Counts + summary still refresh; the list re-renders on
    // the next tick after the decision settles and clears decidingIds.
    const editing = isEditingText() || state.decidingIds.size > 0;
    const scrollEl = $("#content");
    const scrollTop = scrollEl ? scrollEl.scrollTop : 0;
    try {
      if (state.view === "home") {
        const openIds = openDetailIds($("#home-body"));
        await pollHome(editing);
        if (!editing) restoreOpenDetails($("#home-body"), openIds);
      } else {
        const openIds = openDetailIds($("#approvals-body"));
        await pollApprovals(editing);
        if (!editing) restoreOpenDetails($("#approvals-body"), openIds);
      }
      if (scrollEl) scrollEl.scrollTop = scrollTop;
    } catch { /* transient; try again next tick */ }
    finally { state.polling = false; }
  }

  /** Home poll: refresh counts + summary always; re-render lists unless the
      user is editing text (then we leave the DOM untouched). */
  async function pollHome(editing) {
    let home;
    try { home = await api("/api/home"); }
    catch { return; }
    const pending = Array.isArray(home.pending) ? home.pending : [];
    const legacy = Array.isArray(home.legacy) ? home.legacy : [];
    const referralsNew = Array.isArray(home.referrals_new) ? home.referrals_new : [];
    const inboxNew = Array.isArray(home.inbox_new) ? home.inbox_new : [];

    // Reconcile optimistic removals: keep only ids the backend STILL reports
    // as pending (so a card resurrected by the backend reappears, and a
    // decided card stays gone once the backend drops it).
    reconcileOptimistic(pending.concat(legacy));

    updateNavCounts(pending.length, referralsNew.length, inboxNew.length);
    renderSummaryPanel($("#home-summary"), pending);

    // A full loadHome() rebuilds every home section; only do it when the user
    // isn't mid-edit. Reuse the payload we just fetched (no second request).
    // loadHome re-runs updateNavCounts + renderSummaryPanel — idempotent.
    if (!editing) await loadHome(home);
  }

  /** Approvals poll: refresh the list (respecting the active filter) + panel. */
  async function pollApprovals(editing) {
    // Always refresh the honest pending panel.
    const status = $("#approvals-filter").value;
    let list;
    try {
      const qs = status ? `?status=${encodeURIComponent(status)}` : "";
      list = await api("/api/approvals" + qs);
    } catch { return; }
    let cards = Array.isArray(list) ? list : (list.approvals || []);

    // Reconcile against real pending state for the summary + optimistic set.
    if (status === "pending") {
      reconcileOptimistic(cards);
      renderSummaryPanel($("#approvals-summary"), cards);
    } else {
      refreshApprovalsSummary();
    }
    refreshCounts();

    if (editing) return;   // don't disturb an in-progress note

    cards = cards.filter((c) => !state.optimisticGone.has(String(c.id)));
    const body = $("#approvals-body");
    body.replaceChildren();
    if (!cards.length) {
      body.appendChild(txt("div", "empty", "No approvals for this filter."));
      return;
    }
    cards.forEach((c) => body.appendChild(renderApprovalCard(c)));
  }

  /**
   * Trim state.optimisticGone down to ids the backend STILL lists as pending.
   * Once the backend stops returning a decided card as pending, we no longer
   * need to hide it (it's genuinely gone), so we drop it from the set. This
   * guarantees the set never keeps a card hidden contrary to real state.
   */
  function reconcileOptimistic(pendingCards) {
    if (!state.optimisticGone.size) return;
    const stillPending = new Set(
      (pendingCards || [])
        .filter((c) => (c.status || "pending").toLowerCase() === "pending")
        .map((c) => String(c.id))
    );
    state.optimisticGone.forEach((id) => {
      if (!stillPending.has(id)) state.optimisticGone.delete(id);
    });
    // Decisions that have fully settled can also clear from decidingIds.
    state.decidingIds.forEach((id) => {
      if (!stillPending.has(id)) state.decidingIds.delete(id);
    });
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(pollTick, POLL_MS);
  }
  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  /* ================================================================
     BOOT
     ================================================================ */
  function wireEvents() {
    $("#login-form").addEventListener("submit", doLogin);
    $("#logout-btn").addEventListener("click", doLogout);

    $$(".nav-item").forEach((b) =>
      b.addEventListener("click", () => setView(b.dataset.view))
    );

    $("#run-manager-btn").addEventListener("click", (e) =>
      runManager(e.currentTarget));
    $("#ingest-btn").addEventListener("click", (e) =>
      ingestReferrals(e.currentTarget));
    $("#referral-audit-btn").addEventListener("click", (e) =>
      runReferralAudit(e.currentTarget));

    $("#approvals-filter").addEventListener("change", loadApprovals);
    $("#referrals-filter").addEventListener("change", loadReferrals);

    // Inbox
    $("#inbox-poll-btn").addEventListener("click", (e) =>
      pollInbox(e.currentTarget));
    $("#inbox-channel-filter").addEventListener("change", loadInbox);
    $("#inbox-status-filter").addEventListener("change", loadInbox);

    $$("[data-refresh]").forEach((b) =>
      b.addEventListener("click", () => setView(b.dataset.refresh))
    );

    // Chat
    $("#chat-form").addEventListener("submit", sendChat);
    const ci = $("#chat-input");
    ci.addEventListener("input", () => autoGrow(ci));
    ci.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });

    // Live sync: pause when the tab is hidden, and do an immediate refresh
    // the moment it becomes visible / regains focus (so it feels connected).
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) pollTick();
    });
    window.addEventListener("focus", () => pollTick());
  }

  async function boot() {
    wireEvents();
    // Determine session state via /api/me (401 -> login view).
    try {
      const me = await api("/api/me");
      state.user = { username: me.username, role: me.role };
      renderWhoAmI();
      showApp();
      await checkHealth();
      setView("home");
      startPolling();
    } catch (err) {
      // Unauthorized already routed to login; other errors -> login too.
      if (!(err instanceof Unauthorized)) showLogin();
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

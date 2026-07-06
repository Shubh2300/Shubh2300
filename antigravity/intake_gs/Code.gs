/**
 * intake_gs/Code.gs - Atlantic Pain & Wellness Institute
 * Production patient-referral intake for Google Apps Script (V8 runtime).
 *
 * Drop-in upgrade for the existing Code.js/Config.js intake script.
 * Uses the SAME Script Property names; new ones are optional with defaults.
 *
 * IMPORTANT - only ONE intake script should have an active time trigger.
 * If Code.js is still present in the project, delete its trigger before
 * enabling the trigger for processInbox here.
 *
 * Deployment:
 *   1. Paste this file (Code.gs) into the Apps Script editor - replace old code.
 *   2. Enable Drive API advanced service (v3).
 *   3. Set Script Properties (see README.md for full table).
 *   4. Run runUnitTests() -> all should PASS.
 *   5. Run dryRun() to confirm parsing without side effects.
 *   6. Enable a time-based trigger pointing to processInbox.
 */

'use strict';

// ---------------------------------------------------------------------------
// # Config loading
// ---------------------------------------------------------------------------

/**
 * Load all config from PropertiesService.getScriptProperties() once per run.
 * @returns {Object}
 */
function loadConfig_() {
  const raw = PropertiesService.getScriptProperties().getProperties();
  const cfg = {};

  // Existing property names - keep unchanged
  cfg.LOG_SHEET_ID          = raw.LOG_SHEET_ID          || '';
  cfg.PATIENTS_ROOT_FOLDER_ID = raw.PATIENTS_ROOT_FOLDER_ID || '';
  cfg.QUARANTINE_FOLDER_ID  = raw.QUARANTINE_FOLDER_ID  || '';
  cfg.LOG_DOC_ID            = raw.LOG_DOC_ID            || '';
  cfg.GEMINI_API_KEY        = raw.GEMINI_API_KEY        || '';
  cfg.INBOX_QUERY           = raw.INBOX_QUERY           || 'in:inbox';
  cfg.PROCESSED_LABEL       = raw.PROCESSED_LABEL       || 'patient-pdfs-processed';
  cfg.REVIEW_LABEL          = raw.REVIEW_LABEL          || 'patient-pdfs-needs-review';
  cfg.FEE_SLIP_TEMPLATE_ID  = raw.FEE_SLIP_TEMPLATE_ID  || '';
  cfg.WC_FORM_TEMPLATE_ID   = raw.WC_FORM_TEMPLATE_ID   || '';
  cfg.INTAKE_FORM_TEMPLATE_ID = raw.INTAKE_FORM_TEMPLATE_ID || '';

  // New optional properties
  cfg.IGNORED_LABEL         = raw.IGNORED_LABEL         || 'patient-pdfs-ignored';
  cfg.RECORDS_LABEL         = raw.RECORDS_LABEL         || 'patient-records-billing';
  cfg.REPORTS_LABEL         = raw.REPORTS_LABEL         || 'patient-reports-inbound';
  cfg.SKIP_VENDOR_INVOICES  = (raw.SKIP_VENDOR_INVOICES  || 'true') !== 'false';
  cfg.DRY_RUN               = (raw.DRY_RUN               || 'false') === 'true';
  cfg.GEMINI_MODEL          = raw.GEMINI_MODEL           || 'gemini-1.5-flash';
  cfg.MAX_THREADS           = parseInt(raw.MAX_THREADS   || '25', 10);
  cfg.SWEEP_MAX_THREADS     = parseInt(raw.SWEEP_MAX_THREADS || '120', 10);
  cfg.TIME_BUDGET_MS        = parseInt(raw.TIME_BUDGET_MS || '300000', 10);
  cfg.FOLDER_NAME_STYLE     = raw.FOLDER_NAME_STYLE      || 'paren-dob';

  // Staff blocklist: comma-separated full names, normalised to lower-case
  const rawBlocklist = raw.STAFF_NAME_BLOCKLIST || '';
  cfg.STAFF_NAME_BLOCKLIST  = rawBlocklist
    .split(',')
    .map(s => s.trim().toLowerCase())
    .filter(Boolean);

  // Draft email generation (CHANGE A)
  cfg.DRAFT_PATIENT_EMAILS    = (raw.DRAFT_PATIENT_EMAILS    || 'true') !== 'false';
  cfg.PATIENT_FORM_URL        = raw.PATIENT_FORM_URL || '';

  // Auto-draft records/billing reply for staff to verify (CHANGE A)
  cfg.DRAFT_RECORDS_REPLIES   = (raw.DRAFT_RECORDS_REPLIES   || 'true') !== 'false';

  // New optional properties
  cfg.PATIENT_TRACKER_SHEET_ID = raw.PATIENT_TRACKER_SHEET_ID || '';
  cfg.PATIENT_TRACKER_TAB      = raw.PATIENT_TRACKER_TAB      || 'All WC Patients';

  // OpenAI (primary AI provider when key is set; Gemini is the fallback)
  cfg.OPENAI_API_KEY = raw.OPENAI_API_KEY || '';
  cfg.OPENAI_MODEL   = raw.OPENAI_MODEL   || 'gpt-4o-mini';

  // Instant new-referral alert ("call to book") - separate from the daily digest
  cfg.NOTIFY_ON_REFERRAL = (raw.NOTIFY_ON_REFERRAL || '1') !== '0';   // default ON
  cfg.NOTIFY_EMAIL       = raw.NOTIFY_EMAIL || '';                    // empty -> send to self (effective user)

  // Deterministic sender rules (config-driven)
  cfg.PARTNER_BILLING_DOMAINS = (raw.PARTNER_BILLING_DOMAINS || 'mdmanage.com,srm-inc.com').toLowerCase();
  cfg.IGNORE_SENDERS = (raw.IGNORE_SENDERS || 'invoice+statements@mail.anthropic.com,failed-payments@mail.anthropic.com,noreply-apps-scripts-notifications@google.com,alerts@tdbank.com,noreply@messaging.squareup.com').toLowerCase();

  // Booking-request lane + own-address detection (thread follow-up awareness)
  cfg.BOOKING_LABEL    = raw.BOOKING_LABEL    || 'Intake/Booking Requests';
  cfg.CLINIC_ADDRESSES = (raw.CLINIC_ADDRESSES || 'mainlinesurgery@gmail.com,mainlinepain@gmail.com,mainsurgical@gmail.com').toLowerCase();

  return cfg;
}

// ---------------------------------------------------------------------------
// # Ledger helpers (Runs sheet in LOG_SHEET_ID)
// ---------------------------------------------------------------------------

const LEDGER_HEADERS = ['Timestamp', 'MessageId', 'Status', 'Patient', 'Confidence', 'FolderUrl', 'Detail'];

/**
 * Get or create the 'Runs' sheet; return a JS Map of messageId -> {rowIndex, status}.
 * Reads the sheet ONCE per run; callers mutate this in-memory cache.
 * @param {SpreadsheetApp.Spreadsheet} ss
 * @returns {{ sheet: SpreadsheetApp.Sheet, cache: Map<string,{row:number,status:string}> }}
 */
function openLedger_(ss) {
  let sheet = ss.getSheetByName('Runs');
  if (!sheet) {
    sheet = ss.insertSheet('Runs');
    sheet.appendRow(LEDGER_HEADERS);
    sheet.setFrozenRows(1);
  }
  const lastRow = sheet.getLastRow();
  const cache = new Map();
  if (lastRow > 1) {
    const data = sheet.getRange(2, 1, lastRow - 1, LEDGER_HEADERS.length).getValues();
    data.forEach((row, i) => {
      const msgId  = String(row[1]);
      const status = String(row[2]);
      if (msgId) cache.set(msgId, { row: i + 2, status });
    });
  }
  return { sheet, cache };
}

/**
 * Claim a ledger row for this messageId. If the cache already has a row for
 * this messageId (e.g. status 'retry' from retryReviewQueue_), REUSE that
 * row - set its status cell back to 'claimed' in place - instead of
 * appending a duplicate row. New messageIds keep the append behavior.
 * Return the row number.
 */
function ledgerClaim_(sheet, messageId, cache) {
  const existing = cache.get(messageId);
  if (existing) {
    ledgerUpdate_(sheet, existing.row, 'claimed', '', '', '', '');
    cache.set(messageId, { row: existing.row, status: 'claimed' });
    return existing.row;
  }
  const ts = new Date().toISOString();
  sheet.appendRow([ts, messageId, 'claimed', '', '', '', '']);
  const rowNum = sheet.getLastRow();
  cache.set(messageId, { row: rowNum, status: 'claimed' });
  return rowNum;
}

/** Update an existing ledger row in-place. */
function ledgerUpdate_(sheet, rowNum, status, patient, confidence, folderUrl, detail) {
  sheet.getRange(rowNum, 1, 1, LEDGER_HEADERS.length).setValues([[
    new Date().toISOString(),
    sheet.getRange(rowNum, 2).getValue(),  // preserve messageId
    status,
    patient    || '',
    confidence || '',
    folderUrl  || '',
    detail     || ''
  ]]);
}

/**
 * True when a ledger status means "this message is fully handled - do not
 * re-process it". Every status ledgerUpdate_ is ever called with EXCEPT
 * 'claimed' (in-flight) and 'retry' (deliberately re-opened by
 * retryReviewQueue_) is terminal:
 *   done, ignored, records, report-filed, review, error, stale-claim
 * Empty/undefined status (no ledger row yet) is NOT terminal.
 * Pure function - no Apps Script globals - unit-testable in Node.
 * @param {string} status
 * @returns {boolean}
 */
function isTerminalLedgerStatus_(status) {
  if (!status) return false;
  return status !== 'claimed' && status !== 'retry';
}

// ---------------------------------------------------------------------------
// # Entry points
// ---------------------------------------------------------------------------

/**
 * processInbox - main time-trigger entry point.
 * Claim-first resilience: write 'claimed' BEFORE processing, update after.
 */
function processInbox() {
  const startMs  = Date.now();
  const cfg      = loadConfig_();
  const bulletin = [];

  if (!cfg.OPENAI_API_KEY && !cfg.GEMINI_API_KEY) {
    Logger.log('FATAL: no AI key set in Script Properties. Set OPENAI_API_KEY or GEMINI_API_KEY.');
    return;
  }
  if (!cfg.LOG_SHEET_ID) {
    Logger.log('FATAL: LOG_SHEET_ID not set in Script Properties.');
    return;
  }

  const ss = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
  const { sheet: ledgerSheet, cache: ledgerCache } = openLedger_(ss);

  const threads = GmailApp.search(cfg.INBOX_QUERY, 0, cfg.MAX_THREADS);
  Logger.log(`processInbox: found ${threads.length} thread(s) to evaluate.`);

  let deferred = 0;
  for (let t = 0; t < threads.length; t++) {
    // Time-budget guard
    if (Date.now() - startMs > cfg.TIME_BUDGET_MS) {
      deferred = threads.length - t;
      Logger.log(`Time budget exceeded - deferring ${deferred} thread(s) to next run.`);
      var deferredSubjects = threads.slice(t, t + 5).map(safeSubject_);
      bulletin.push(`[TIME] Deferred ${deferred} thread(s) (time budget ${cfg.TIME_BUDGET_MS} ms): ${deferredSubjects.join(' | ')}${deferred > deferredSubjects.length ? ' ...' : ''}`);
      break;
    }

    const thread = threads[t];
    try {
      processThread_(thread, cfg, ledgerSheet, ledgerCache, bulletin);
    } catch (err) {
      const subj = safeSubject_(thread);
      Logger.log(`UNCAUGHT error on thread "${subj}": ${err}`);
      bulletin.push(`[ERR] Uncaught error - "${subj}": ${err}`);
      try { applyLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    }
  }

  // Write bulletin to log doc
  if (cfg.LOG_DOC_ID) {
    try { writeToDocBulletin_(bulletin, cfg); } catch (e) {
      Logger.log(`Bulletin write failed: ${e}`);
    }
  }
  try { writeRunSummary_(cfg, startMs, threads.length, false); } catch (_) {}
  Logger.log('processInbox complete.');
}

/**
 * sweepBacklog - daily trigger entry point. Scans a wider window (SWEEP_MAX_THREADS)
 * to catch any emails the 30-min live scan missed. The claim-first ledger makes this
 * fully idempotent: already-processed message IDs are skipped automatically.
 * Intended use: add a daily time-based trigger pointing to sweepBacklog.
 */
function sweepBacklog() {
  const startMs  = Date.now();
  const cfg      = loadConfig_();
  const bulletin = [];

  if (!cfg.OPENAI_API_KEY && !cfg.GEMINI_API_KEY) {
    Logger.log('FATAL: no AI key set in Script Properties. Set OPENAI_API_KEY or GEMINI_API_KEY.');
    return;
  }
  if (!cfg.LOG_SHEET_ID) {
    Logger.log('FATAL: LOG_SHEET_ID not set in Script Properties.');
    return;
  }

  const ss = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
  const { sheet: ledgerSheet, cache: ledgerCache } = openLedger_(ss);

  const threads = GmailApp.search(cfg.INBOX_QUERY, 0, cfg.SWEEP_MAX_THREADS);
  Logger.log(`sweepBacklog: found ${threads.length} thread(s) to evaluate.`);

  let deferred = 0;
  for (let t = 0; t < threads.length; t++) {
    // Time-budget guard
    if (Date.now() - startMs > cfg.TIME_BUDGET_MS) {
      deferred = threads.length - t;
      Logger.log(`sweepBacklog: time budget exceeded - deferring ${deferred} thread(s).`);
      var deferredSubjectsSweep = threads.slice(t, t + 5).map(safeSubject_);
      bulletin.push(`[TIME] Deferred ${deferred} thread(s) (time budget ${cfg.TIME_BUDGET_MS} ms): ${deferredSubjectsSweep.join(' | ')}${deferred > deferredSubjectsSweep.length ? ' ...' : ''}`);
      break;
    }

    const thread = threads[t];
    try {
      processThread_(thread, cfg, ledgerSheet, ledgerCache, bulletin);
    } catch (err) {
      const subj = safeSubject_(thread);
      Logger.log(`sweepBacklog: UNCAUGHT error on thread "${subj}": ${err}`);
      bulletin.push(`[ERR] Uncaught error - "${subj}": ${err}`);
      try { applyLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    }
  }

  // Write bulletin to log doc
  if (cfg.LOG_DOC_ID) {
    try { writeToDocBulletin_(bulletin, cfg); } catch (e) {
      Logger.log(`sweepBacklog: bulletin write failed: ${e}`);
    }
  }
  try { writeRunSummary_(cfg, startMs, threads.length, true); } catch (_) {}
  Logger.log('sweepBacklog complete.');
}

/**
 * retryReviewQueue - manually callable from the Apps Script editor. Run once
 * after the clinic fixes an AI-quota problem (e.g. adds OPENAI_API_KEY) to
 * re-open ledger items that were parked in 'review', 'error', or
 * 'stale-claim'. Flips each row's status to 'retry'; the next processInbox
 * or sweepBacklog run will re-attempt those messages (isTerminalLedgerStatus_
 * treats 'retry' as non-terminal, and ledgerClaim_ reuses the existing row).
 *
 * Does NOT call any AI or Gmail APIs itself - it only flips ledger statuses.
 *
 * NOTE: retried items may produce duplicate Gmail drafts if the item had
 * partially processed before being parked (e.g. a records-reply draft was
 * already created). That is acceptable for a deliberate manual retry - a
 * human explicitly asked for these to be re-attempted.
 */
function retryReviewQueue() {
  const cfg = loadConfig_();
  if (!cfg.LOG_SHEET_ID) {
    Logger.log('retryReviewQueue: LOG_SHEET_ID not set.');
    return;
  }

  const ss = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
  const { sheet: ledgerSheet } = openLedger_(ss);

  const lastRow = ledgerSheet.getLastRow();
  if (lastRow < 2) {
    Logger.log('retryReviewQueue: Runs sheet has no data rows.');
    return;
  }

  const RETRYABLE_STATUSES = ['review', 'error', 'stale-claim'];
  let reopened = 0;
  for (let row = 2; row <= lastRow; row++) {
    const statusCell = ledgerSheet.getRange(row, 3); // Status column
    const status = String(statusCell.getValue());
    if (RETRYABLE_STATUSES.indexOf(status) !== -1) {
      statusCell.setValue('retry');
      reopened++;
    }
  }

  Logger.log(`retryReviewQueue: re-opened ${reopened} row(s) (status -> 'retry'). ` +
             'The next processInbox/sweepBacklog run will re-attempt them.');
}

/**
 * dryRun - manually callable; processes the newest matching thread through
 * classification + parsing ONLY. No labels, no folders, no docs, no ledger writes.
 */
function dryRun() {
  const cfg = loadConfig_();
  const threads = GmailApp.search(cfg.INBOX_QUERY, 0, 1);
  if (!threads.length) {
    Logger.log('dryRun: no threads match INBOX_QUERY.');
    return;
  }
  const thread  = threads[0];
  const messages = thread.getMessages();
  const msg      = messages[messages.length - 1];
  const subject  = msg.getSubject() || '';
  const body     = msg.getPlainBody() || '';

  Logger.log(`dryRun: subject="${subject}"`);

  // Gate 1
  if (isBulkMail_(msg)) {
    Logger.log('dryRun: WOULD IGNORE - bulk/marketing mail.');
    return;
  }
  if (cfg.SKIP_VENDOR_INVOICES && isVendorInvoice_(subject, body)) {
    Logger.log('dryRun: WOULD IGNORE - vendor invoice filter.');
    return;
  }

  // Gate 2
  const parsed = parsePatientInfoFromText_(subject, body);
  Logger.log(`dryRun: parsed name="${parsed.firstName} ${parsed.lastName}" dob="${parsed.dob}" confidence="${parsed.confidence}" dobSource="${parsed.dobSource}"`);

  // Gate 3
  const classification = classifyReferralWithGemini_(subject, body, cfg);
  Logger.log(`dryRun: Gemini isReferral=${JSON.stringify(classification.isReferral)} reason="${classification.reason || ''}"`);

  Logger.log('dryRun complete - no side effects applied.');
}

// ---------------------------------------------------------------------------
// # Thread processing
// ---------------------------------------------------------------------------

function safeSubject_(thread) {
  try { return thread.getFirstMessageSubject(); } catch (_) { return '(unknown)'; }
}

/**
 * Core per-thread handler - claim-first pattern.
 */
function processThread_(thread, cfg, ledgerSheet, ledgerCache, bulletin) {
  const messages = thread.getMessages();
  const msg      = messages[messages.length - 1];
  const msgId    = msg.getId();
  const subject  = msg.getSubject() || '';

  // Check ledger
  const existing = ledgerCache.get(msgId);
  if (existing) {
    if (existing.status === 'claimed') {
      Logger.log(`Stale claim on ${msgId} - marking for review.`);
      ledgerUpdate_(ledgerSheet, existing.row, 'stale-claim', '', '', '', 'Previous run claimed but never completed');
      ledgerCache.set(msgId, { row: existing.row, status: 'stale-claim' });
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push(`[WARN] Stale claim - "${subject}" queued for manual review.`);
      return;
    } else if (isTerminalLedgerStatus_(existing.status)) {
      Logger.log(`Skipping ${msgId} - already ${existing.status}.`);
      return;
    } else if (existing.status === 'retry') {
      Logger.log(`Re-processing ${msgId} - status was 'retry' (manual retryReviewQueue_).`);
      // fall through to normal processing below
    }
  }

  // Gate 0 - Sway head-injury test reports (self-sent from the Sway app). Deterministic, no AI.
  var swayAttachments = [];
  try { swayAttachments = msg.getAttachments(); } catch (_) {}
  var hasSwayPdf = swayAttachments.some(function(a) {
    return isSwayReportName_(a.getName()) && (a.getContentType() || '').toLowerCase().indexOf('pdf') !== -1;
  });
  if (isSwayReportName_(subject) || hasSwayPdf) {
    Logger.log('Sway head-injury test detected: "' + subject + '"');
    var swayRow = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
    routeSwayReport_(msg, thread, cfg, ledgerSheet, swayRow, bulletin);
    return;
  }

  const body = msg.getPlainBody() || '';

  // Thread follow-up awareness (the Damon Holden fix): a reply landing on an
  // already-labeled patient thread must never be classified in isolation. If
  // the thread already carries a lane label, either log it as a tracked
  // follow-up (inbound) or silently note staff's own reply (outbound) -
  // never re-run the gates below on it.
  var threadLabelNames = [];
  try { threadLabelNames = thread.getLabels().map(function (l) { return l.getName(); }); } catch (_) { threadLabelNames = []; }
  var laneMap = [[cfg.RECORDS_LABEL, 'records'], [cfg.BOOKING_LABEL, 'booking'], [cfg.PROCESSED_LABEL, 'referral'], [cfg.REPORTS_LABEL, 'report']];
  var priorLane = null;
  for (var li = 0; li < laneMap.length; li++) {
    if (threadLabelNames.indexOf(laneMap[li][0]) !== -1) { priorLane = laneMap[li][1]; break; }
  }
  if (priorLane) {
    if (!isOwnAddress_(msg.getFrom(), cfg.CLINIC_ADDRESSES) && !isIntakeAlertSubject_(subject)) {
      var fupRow = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
      routeThreadFollowup_(msg, thread, parsePatientInfoFromText_(subject, body), subject, cfg, ledgerSheet, fupRow, bulletin, priorLane);
      return;
    }
    if (isOwnAddress_(msg.getFrom(), cfg.CLINIC_ADDRESSES)) {
      var ownRow = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
      ledgerUpdate_(ledgerSheet, ownRow, 'ignored', '', '', '', 'own reply on handled thread');
      return;
    }
  }

  // Gate 1 - deterministic, no AI
  if (isIntakeAlertSubject_(subject)) {
    Logger.log(`Self-notification ignored (loop guard): "${subject}"`);
    const row = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
    ledgerUpdate_(ledgerSheet, row, 'ignored', '', '', '', 'self-notification');
    applyLabel_(thread, cfg.IGNORED_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    return;
  }
  if (isIgnoredSender_(msg.getFrom(), cfg.IGNORE_SENDERS)) {
    Logger.log(`Ignored sender: "${subject}"`);
    const row = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
    ledgerUpdate_(ledgerSheet, row, 'ignored', '', '', '', 'ignored sender rule');
    applyLabel_(thread, cfg.IGNORED_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    return;
  }
  // RingCentral fax/voicemail notifications: the document/audio is NOT attached to
  // these emails, so queue them to the Fax Queue tab for human triage instead of
  // silently ignoring or (worse) treating them as a patient referral.
  if (String(msg.getFrom() || '').toLowerCase().indexOf('notify@ringcentral.com') !== -1) {
    var faxInfo = parseFaxNotification_(subject, body);
    var voiceMatch = !faxInfo ? /^New Voice Message from (.+) on /i.exec(subject) : null;
    if (faxInfo || voiceMatch) {
      const row = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
      const fromNumber = faxInfo ? faxInfo.fromNumber : voiceMatch[1].trim();
      const pages = faxInfo ? faxInfo.pages : 0;
      const kind = faxInfo ? 'fax' : 'voicemail';
      try {
        const ss  = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
        const tab = getOrCreateTab_(ss, 'Fax Queue', ['Date', 'From Number', 'Pages', 'Subject', 'MessageId', 'Status']);
        tab.appendRow([new Date().toISOString(), fromNumber, pages, subject, msgId, 'NEW']);
      } catch (e) {
        Logger.log(`RingCentral ${kind} queue logging error - ${e}`);
      }
      const detail = faxInfo
        ? `fax notification - ${pages} pages from ${fromNumber}`
        : 'voicemail notification';
      ledgerUpdate_(ledgerSheet, row, 'fax-queued', '', '', '', detail);
      applyLabel_(thread, cfg.REVIEW_LABEL);
      if (faxInfo) {
        bulletin.push(`[FAX] ${pages}-page fax from ${fromNumber} -> Fax Queue (document lives in RingCentral portal).`);
      } else {
        bulletin.push(`[FAX] Voicemail from ${fromNumber} -> Fax Queue (audio lives in RingCentral portal).`);
      }
      return;
    }
  }
  if (isBulkMail_(msg)) {
    Logger.log(`Bulk mail ignored: "${subject}"`);
    const row = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
    ledgerUpdate_(ledgerSheet, row, 'ignored', '', '', '', 'bulk/marketing mail');
    applyLabel_(thread, cfg.IGNORED_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    return;
  }
  if (cfg.SKIP_VENDOR_INVOICES && isVendorInvoice_(subject, body)) {
    Logger.log(`Vendor invoice ignored: "${subject}"`);
    const row = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
    ledgerUpdate_(ledgerSheet, row, 'ignored', '', '', '', 'vendor invoice filter');
    applyLabel_(thread, cfg.IGNORED_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    return;
  }
  if (isSalesSolicitation_(subject, body)) {
    Logger.log(`Sales solicitation ignored: "${subject}"`);
    const row = ledgerClaim_(ledgerSheet, msgId, ledgerCache);
    ledgerUpdate_(ledgerSheet, row, 'ignored', '', '', '', 'sales/marketing solicitation');
    applyLabel_(thread, cfg.IGNORED_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    return;
  }

  // Claim the row before any AI/Drive work
  const claimRow = ledgerClaim_(ledgerSheet, msgId, ledgerCache);

  // Gate 2 - regex identity parsing
  const parsed = parsePatientInfoFromText_(subject, body);

  // Staff blocklist -> route to review immediately
  if (parsed.confidence === 'blocked') {
    Logger.log(`Staff blocklist match on "${subject}" - routing to review.`);
    ledgerUpdate_(ledgerSheet, claimRow, 'review', `${parsed.firstName} ${parsed.lastName}`, 'blocked', '', 'parsed name matches staff blocklist');
    applyLabel_(thread, cfg.REVIEW_LABEL);
    bulletin.push(`[ERR] Staff blocklist - "${subject}" routed to review.`);
    return;
  }

  // Deterministic billing-partner domain routing (no AI). Third-party billing partners
  // (e.g. MDManage, SRM) email from a known domain about an existing patient's
  // records/billing - route straight to the records/billing lane rather than risking
  // an AI misclassification of a partner-domain email.
  if (isPartnerBillingSender_(msg.getFrom(), cfg.PARTNER_BILLING_DOMAINS)) {
    Logger.log(`Billing-partner domain sender on "${subject}" - routing to records/billing lane.`);
    bulletin.push('[REC] billing-partner domain');
    routeRecordsBillingRequest_(msg, thread, parsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, 'billing partner domain');
    return;
  }

  // Deterministic referral fast-path (no AI). A self-labeled "referral" email that
  // carries PDF records is unambiguously a new-patient referral, so process it without
  // Gemini -- this keeps obvious referrals from being stranded in review when the AI is
  // rate-limited. fullProcess_ recovers the patient's name + DOB from the OCR'd packet
  // when the email body alone is insufficient, and quarantines (no folder) if it cannot.
  let hasPdfAttachment = false;
  try {
    hasPdfAttachment = msg.getAttachments().some(function (a) {
      return (a.getContentType() || '').toLowerCase().indexOf('pdf') !== -1;
    });
  } catch (_) {}
  if (isObviousReferralSubject_(subject) && hasPdfAttachment) {
    Logger.log('Obvious referral (subject + PDF) -> deterministic full process: "' + subject + '"');
    bulletin.push('[REC] Referral (no AI) - "' + subject + '"');
    fullProcess_(msg, thread, parsed, cfg, ledgerSheet, claimRow, ledgerCache, bulletin);
    return;
  }

  // Attachment-content referral detection (no AI). Some referrals arrive as a photo or
  // scan with an unhelpful subject ("Document", "Scan"). OCR the attachments and, if the
  // document reads like a referral AND yields a confident patient identity, process it
  // deterministically so it is never stranded in review when the AI is rate-limited.
  var ocrAttachments = [];
  try { ocrAttachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true }); } catch (_) {}
  var hasOcrable = ocrAttachments.some(function (a) { return isOcrableAttachment_(a.getContentType()); });
  if (hasOcrable) {
    var ocrCombined = body;
    try {
      var ocrCacheTab = getOrCreateTab_(SpreadsheetApp.openById(cfg.LOG_SHEET_ID), 'OcrCache', ['Hash', 'ExtractedChars', 'FirstSeen']);
      for (var oi = 0; oi < ocrAttachments.length; oi++) {
        if (!isOcrableAttachment_(ocrAttachments[oi].getContentType())) continue;
        try {
          var t = extractTextFromPdf_(ocrAttachments[oi].copyBlob(), msgId, ocrCacheTab);
          if (t) ocrCombined += '\n' + t;
        } catch (e2) { Logger.log('attachment OCR scan failed: ' + e2); }
      }
    } catch (e3) { Logger.log('attachment OCR scan setup failed: ' + e3); }

    if (looksLikeReferralDoc_(ocrCombined)) {
      // Photographed/scanned referral: detection is reliable, but reading the patient
      // name/DOB off a phone photo of a form is NOT (it misread "Shakirah Smith" as
      // "Ky London"). If any triggering attachment is an image, file it and route to
      // Needs Review for a human to confirm the patient -- never auto-create a chart
      // from an untrusted OCR name (honest-data house rule). PDF referrals fall through
      // to the normal auto-process path below.
      var hasImageAttachment = ocrAttachments.some(function (a) {
        return (a.getContentType() || '').toLowerCase().indexOf('image/') === 0;
      });
      if (hasImageAttachment) {
        quarantinePdfs_(msg, cfg);
        var guess = parsePatientInfoFromText_(subject, ocrCombined);
        var guessName = (guess.firstName + ' ' + guess.lastName).trim();
        ledgerUpdate_(ledgerSheet, claimRow, 'review', guessName, guess.confidence || '', '', 'photographed/scanned referral - verify patient identity (OCR name unreliable)');
        applyLabel_(thread, cfg.REVIEW_LABEL);
        try { removeLabel_(thread, cfg.PROCESSED_LABEL); } catch (_) {}
        bulletin.push('[WARN] Photo referral detected - "' + subject + '" routed to review; a human should confirm the patient' + (guessName ? ' (best guess: ' + guessName + ')' : '') + '.');
        return;
      }
      var parsedFromDoc = parsePatientInfoFromText_(subject, ocrCombined);
      if (parsedFromDoc.confidence === 'high') {
        Logger.log('Referral detected from attachment content (no AI): "' + subject + '"');
        bulletin.push('[REC] Referral from attachment (no AI) - "' + subject + '"');
        fullProcess_(msg, thread, parsedFromDoc, cfg, ledgerSheet, claimRow, ledgerCache, bulletin, ocrCombined);
        return;
      }
      // Reads like a referral but we could not confirm name + DOB -> quarantine, do not drop.
      quarantinePdfs_(msg, cfg);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', (parsedFromDoc.firstName + ' ' + parsedFromDoc.lastName).trim(), parsedFromDoc.confidence || '', '', 'referral-like attachment but no confident name+DOB; quarantined');
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Referral-like attachment - could not confirm identity; "' + subject + '" quarantined.');
      return;
    }
  }

  // Deterministic booking/scheduling request (no AI). Records/billing keeps priority
  // (isBookingRequest_ already excludes that overlap). Route to the booking queue so
  // "call to schedule" requests are never stranded without a lane or an outcome
  // (the Damon Holden / Harun Omar Sahin gap this lane exists to close).
  if (isBookingRequest_(subject, body)) {
    var bookParsed = parsed;
    var hasCapitalizedName = /\b[A-Z][a-z]+\s+[A-Z][a-z]+\b/.test(subject);
    if (bookParsed.lastName || hasCapitalizedName) {
      if (!bookParsed.lastName && hasCapitalizedName) {
        var byPhraseBook = extractRequestedPatientName_(subject, body);
        bookParsed = { firstName: byPhraseBook.firstName, lastName: byPhraseBook.lastName, dob: '',
                       confidence: (byPhraseBook.firstName || byPhraseBook.lastName) ? 'low' : 'none', dobSource: '' };
      }
      Logger.log('Booking request (no AI): "' + subject + '"');
      routeBookingRequest_(msg, thread, bookParsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, 'deterministic booking-request detection');
      return;
    }
    // booking-like but no patient identifiable -> let Gemini/fall-through handle it
  }

  // Deterministic records/billing request (no AI). Detect the request, identify the patient
  // (by full name or last name), and route to the records lane so it is never stranded in
  // review when the AI is rate-limited.
  if (isRecordsOrBillingRequest_(subject, body)) {
    var recParsed = parsePatientInfoFromText_(subject, body);
    if (recParsed.confidence === 'none') {
      var byPhrase = extractRequestedPatientName_(subject, body);
      recParsed = { firstName: byPhrase.firstName, lastName: byPhrase.lastName, dob: '',
                    confidence: (byPhrase.firstName || byPhrase.lastName) ? 'low' : 'none', dobSource: '' };
    }
    if (recParsed.lastName || recParsed.firstName) {
      Logger.log('Records/billing request (no AI): "' + subject + '"');
      routeRecordsBillingRequest_(msg, thread, recParsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, 'deterministic records/billing detection');
      return;
    }
    // records-like but no patient identifiable -> let Gemini/fall-through handle it
  }

  // Deterministic inbound report/result safety lane (no AI). A report/result is
  // patient material even when it is not a new referral. If identity is missing,
  // quarantine and review instead of allowing a non-referral classifier result
  // to send it to Ignored.
  if (isInboundPatientReport_(subject, body)) {
    if (parsed.confidence === 'none') {
      quarantinePdfs_(msg, cfg);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', '', 'none', '',
        'inbound patient report/result but no patient identity parsed; PDFs quarantined');
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Inbound report/result with no patient identity - "' + subject + '" quarantined for review.');
      return;
    }
    routeInboundReport_(msg, thread, parsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, (typeof ocrCombined !== 'undefined' ? ocrCombined : ''));
    return;
  }

  // Gate 3 - AI category router (replaces the old binary referral check).
  // classifyReferralWithGemini_ itself is left in the file - dryRun()/tests may
  // still reference it - but processThread_ now routes by category.
  const category = classifyEmailCategory_(subject, body, cfg);

  if (category === null) {
    // AI failed (both providers, or no key configured) - fail safe, same as before.
    Logger.log(`AI category classification failed on "${subject}" - routing to review.`);
    ledgerUpdate_(ledgerSheet, claimRow, 'review', '', '', '', 'AI classification failure; manual review required');
    applyLabel_(thread, cfg.REVIEW_LABEL);
    bulletin.push(`[ERR] AI classification failure - "${subject}" routed to review.`);
    return;
  }

  switch (category.category) {
    case 'BOOKING_REQUEST': {
      var bookCatParsed = parsed;
      if (bookCatParsed.confidence === 'none' && (category.patientFirst || category.patientLast)) {
        bookCatParsed = { firstName: category.patientFirst || '', lastName: category.patientLast || '',
                           dob: '', confidence: 'low', dobSource: '' };
      }
      routeBookingRequest_(msg, thread, bookCatParsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, 'AI category');
      return;
    }

    case 'RECORDS_REQUEST': {
      var recCatParsed = parsed;
      if (recCatParsed.confidence === 'none' && (category.patientFirst || category.patientLast)) {
        recCatParsed = { firstName: category.patientFirst || '', lastName: category.patientLast || '',
                          dob: '', confidence: 'low', dobSource: '' };
      }
      routeRecordsBillingRequest_(msg, thread, recCatParsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, 'AI category');
      return;
    }

    case 'INBOUND_REPORT': {
      var repCatParsed = parsed;
      if (repCatParsed.confidence === 'none' && (category.patientFirst || category.patientLast)) {
        repCatParsed = { firstName: category.patientFirst || '', lastName: category.patientLast || '',
                          dob: '', confidence: 'low', dobSource: '' };
      }
      if (repCatParsed.confidence === 'none') {
        quarantinePdfs_(msg, cfg);
        ledgerUpdate_(ledgerSheet, claimRow, 'review', '', 'none', '',
          'inbound patient report (AI category) but no patient identity parsed; PDFs quarantined');
        applyLabel_(thread, cfg.REVIEW_LABEL);
        bulletin.push('[WARN] Inbound report (AI category) with no patient identity - "' + subject + '" quarantined for review.');
        return;
      }
      routeInboundReport_(msg, thread, repCatParsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, '');
      return;
    }

    case 'PATIENT_OTHER': {
      Logger.log(`Patient-related, uncategorized: "${subject}" - ${category.reason || ''}`);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', '', '', '', `patient-related (AI: ${category.reason || ''}) - needs a human`);
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Patient-related email needs review');
      return;
    }

    case 'NOT_PATIENT': {
      Logger.log(`Not patient-related: "${subject}" - ${category.reason || ''}`);
      ledgerUpdate_(ledgerSheet, claimRow, 'ignored', '', '', '', `not patient-related: ${category.reason || ''}`);
      applyLabel_(thread, cfg.IGNORED_LABEL);
      try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
      return;
    }

    case 'NEW_REFERRAL':
    default: {
      // NEW_REFERRAL (or an unrecognized category - fail toward the existing
      // referral-confidence path rather than silently dropping the email).
      if (parsed.confidence === 'none') {
        Logger.log(`Referral without parseable name: "${subject}" -> quarantine + review.`);
        quarantinePdfs_(msg, cfg);
        ledgerUpdate_(ledgerSheet, claimRow, 'review', '', 'none', '', 'referral but no patient name parsed');
        applyLabel_(thread, cfg.REVIEW_LABEL);
        bulletin.push(`[WARN] No name parsed - "${subject}" quarantined for review.`);
        return;
      }

      if (parsed.confidence === 'low') {
        Logger.log(`Low-confidence referral (no labeled DOB): "${subject}" -> quarantine + review.`);
        quarantinePdfs_(msg, cfg);
        const nameFull = `${parsed.firstName} ${parsed.lastName}`.trim();
        ledgerUpdate_(ledgerSheet, claimRow, 'review', nameFull, 'low', '', 'name only - no labeled DOB; record not created');
        applyLabel_(thread, cfg.REVIEW_LABEL);
        bulletin.push(`[WARN] Low confidence (no DOB) - "${subject}" quarantined, no records created.`);
        return;
      }

      // High confidence - full processing
      fullProcess_(msg, thread, parsed, cfg, ledgerSheet, claimRow, ledgerCache, bulletin);
      return;
    }
  }
}

// ---------------------------------------------------------------------------
// # Full processing (high-confidence path)
// ---------------------------------------------------------------------------

function fullProcess_(msg, thread, parsed, cfg, ledgerSheet, claimRow, ledgerCache, bulletin, precomputedFullText) {
  const subject  = msg.getSubject() || '';
  let nameFull = capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName);

  let folderUrl = '';
  try {
    // OCR all PDF and image attachments (including inline images)
    const attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });
    let fullText;
    if (typeof precomputedFullText === 'string' && precomputedFullText) {
      fullText = precomputedFullText;
    } else {
      fullText = msg.getPlainBody() || '';
      const ocrCacheSheet = getOrCreateTab_(SpreadsheetApp.openById(cfg.LOG_SHEET_ID), 'OcrCache', ['Hash', 'ExtractedChars', 'FirstSeen']);
      for (const att of attachments) {
        const ct = (att.getContentType() || '').toLowerCase();
        if (!isOcrableAttachment_(ct)) continue;
        try {
          const ocrText = extractTextFromPdf_(att.copyBlob(), msg.getId(), ocrCacheSheet);
          if (ocrText) fullText += '\n' + ocrText;
        } catch (e) {
          Logger.log(`OCR failed for attachment "${att.getName()}": ${e}`);
        }
      }
    }

    // Recover patient identity from the OCR'd packet when the email body alone was
    // insufficient (common for referrals where the name/DOB live in the attached
    // records, not the email text). High-confidence body parse is left untouched.
    if (!parsed || parsed.confidence !== 'high') {
      const fromPacket = parsePatientInfoFromText_(subject, fullText);
      if (fromPacket.confidence === 'high' ||
          (fromPacket.confidence === 'low' && (!parsed || parsed.confidence === 'none'))) {
        parsed = fromPacket;
      }
      nameFull = capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName);
    }

    // Safety guard: never create an empty/junk patient folder. Require a name AND a
    // labeled DOB (high confidence). Otherwise quarantine the PDFs for human review.
    if (parsed.confidence !== 'high') {
      quarantinePdfs_(msg, cfg);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', nameFull.trim(), parsed.confidence || 'low', '',
        'referral packet present but could not extract name + DOB; PDFs quarantined');
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Referral - could not confirm name + DOB from packet; "' + subject + '" quarantined.');
      return;
    }

    // Belt-and-braces: even a "high confidence" parse can be OCR noise (e.g.
    // "Please Hello" pulled from boilerplate PDF text). Never auto-create a
    // chart on an implausible name - fall back to the same quarantine path.
    if (isImplausibleName_(parsed.firstName, parsed.lastName)) {
      quarantinePdfs_(msg, cfg);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', nameFull.trim(), parsed.confidence || 'low', '',
        `implausible parsed name ("${parsed.firstName} ${parsed.lastName}") - manual identity check required`);
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Referral - implausible parsed name ("' + parsed.firstName + ' ' + parsed.lastName + '"); "' + subject + '" quarantined.');
      return;
    }

    // Create patient Drive folder
    const folder = getOrCreatePatientFolder_(parsed.firstName, parsed.lastName, parsed.dob, cfg);
    folderUrl = folder.getUrl();

    // File PDFs and images into folder
    for (const att of attachments) {
      const ct = (att.getContentType() || '').toLowerCase();
      if (isOcrableAttachment_(ct)) {
        filePdfToFolder_(att.copyBlob(), att.getName(), folder, bulletin);
      }
    }

    // Generate intake forms
    generateIntakeForms_({
      firstName:  capitalizeFirst_(parsed.firstName),
      lastName:   capitalizeFirst_(parsed.lastName),
      fullName:   nameFull,
      dob:        parsed.dob
    }, folder.getId(), cfg, bulletin);

    // Head-injury detection (single call; result reused for tracker)
    var isHead = detectHeadInjury_(fullText, cfg);
    if (isHead) {
      const caseType = detectCaseType_(fullText);
      logHeadInjuryPatient_(
        SpreadsheetApp.openById(cfg.LOG_SHEET_ID),
        nameFull, parsed.dob, caseType, folderUrl, msg.getId(), bulletin
      );
    }

    // Gemini administrative summary
    generateGeminiSummary_(fullText, folder.getId(), cfg, nameFull, bulletin);

    // Draft (never auto-send) patient/attorney emails for staff review
    var contacts = {};
    try {
      contacts = extractReferralContacts_(fullText, cfg);
      createReferralDrafts_(parsed, nameFull, contacts, cfg, bulletin);
    } catch (e) {
      Logger.log('Draft email step failed (non-fatal): ' + e);
      bulletin.push('[WARN] Draft email step failed (filing succeeded): ' + e);
    }

    // Add the new patient to the office master tracker sheet
    var trackerType = isHead ? 'Head Injury' : '';
    var trackerWhere = detectCaseType_(fullText);
    logPatientToTracker_(cfg, {
      name:      nameFull,
      folderUrl: folderUrl,
      where:     trackerWhere,
      type:      trackerType,
      phone:     contacts.patientPhone || '',
      referral:  contacts.referral || contacts.referringProvider || '',
      attorney:  (contacts.attorneyName || '') + (contacts.attorneyEmail ? ' <' + contacts.attorneyEmail + '>' : '')
    }, bulletin);

    // Apply processed label; remove review label if present
    applyLabel_(thread, cfg.PROCESSED_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}

    ledgerUpdate_(ledgerSheet, claimRow, 'done', nameFull, 'high', folderUrl, '');
    ledgerCache.set(msg.getId(), { row: claimRow, status: 'done' });
    bulletin.push(`[OK] Done - ${nameFull} (${parsed.dob}) -> ${folderUrl}`);
    Logger.log(`fullProcess_ success: ${nameFull}`);

    // Instant new-referral alert to staff ("call to book"). Never fails the run.
    if (cfg.NOTIFY_ON_REFERRAL) {
      try {
        var alertMsg = buildReferralAlertBody_(
          nameFull, parsed.dob, trackerWhere, isHead,
          contacts.patientPhone || '', contacts.referral || contacts.referringProvider || '',
          folderUrl
        );
        var alertTo = cfg.NOTIFY_EMAIL || Session.getEffectiveUser().getEmail();
        GmailApp.sendEmail(alertTo, alertMsg.subject, alertMsg.plain, { htmlBody: alertMsg.html });
        bulletin.push(`[ALERT] Referral alert sent - ${nameFull} -> ${alertTo}`);
      } catch (e) {
        Logger.log(`fullProcess_: referral alert failed: ${e}`);
        bulletin.push(`[WARN] Referral alert failed: ${e}`);
      }
    }

  } catch (err) {
    Logger.log(`fullProcess_ error for "${subject}": ${err}`);
    ledgerUpdate_(ledgerSheet, claimRow, 'error', nameFull, 'high', folderUrl, String(err));
    applyLabel_(thread, cfg.REVIEW_LABEL);
    bulletin.push(`[ERR] Error - "${subject}": ${err}`);
  }
}

// ---------------------------------------------------------------------------
// # Records / billing routing lane
// ---------------------------------------------------------------------------

/**
 * Build the plain-text and HTML bodies for a records/billing acknowledgment
 * draft reply.  Pure function -- no Apps Script globals -- so it is
 * unit-testable in Node.
 *
 * @param {string} patientName         - Full name, e.g. "Jose Cartagena"
 * @param {string} dob                 - Formatted DOB string or empty string
 * @param {string} reqType             - 'Records' | 'Billing/Insurance'
 * @param {number} docCount            - Number of documents found in the patient folder
 * @param {string} [requestedDoc]      - Specific document type requested (e.g. "EMG"), optional
 * @param {boolean} [requestedDocOnFile] - Whether that specific document was found, optional
 * @returns {{ plain: string, html: string }}
 */
function buildRecordsReplyBody_(patientName, dob, reqType, docCount, requestedDoc, requestedDocOnFile) {
  var nameRef = patientName || '[FILL IN]';
  var dobPart = dob ? ' (DOB ' + dob + ')' : '';

  var intro = 'Good morning,\n\n'
    + 'Thank you for your request regarding ' + nameRef + dobPart + '.\n\n';

  var chartLine;
  if (docCount > 0) {
    chartLine = 'We have located the patient\'s chart. We have ' + docCount
      + ' document(s) on file for this patient.\n\n';
  } else {
    chartLine = 'We are locating the patient\'s chart and will follow up shortly.\n\n';
  }

  // Specific document note (only when a named doc was identified)
  var specificDocLine = '';
  if (requestedDoc) {
    if (requestedDocOnFile) {
      specificDocLine = 'The requested ' + requestedDoc + ' is on file and can be released upon confirmation of a valid signed authorization.\n\n';
    } else {
      specificDocLine = 'We are locating the requested ' + requestedDoc + ' and will follow up.\n\n';
    }
  }

  var actionLine;
  if (reqType === 'Billing/Insurance') {
    actionLine = 'Our billing team will follow up regarding this account.\n\n';
  } else {
    actionLine = 'We will release the requested medical records upon confirmation'
      + ' that a valid signed authorization is on file.\n\n';
  }

  var closing = 'Please let us know if anything further is needed.\n\n'
    + 'Atlantic Pain & Wellness Institute\n'
    + 'Medical Records';

  var plain = intro + chartLine + specificDocLine + actionLine + closing;

  // HTML version: same paragraphs, no styling or external links
  var specificDocHtml = '';
  if (requestedDoc) {
    if (requestedDocOnFile) {
      specificDocHtml = '<p>The requested ' + requestedDoc + ' is on file and can be released upon confirmation of a valid signed authorization.</p>';
    } else {
      specificDocHtml = '<p>We are locating the requested ' + requestedDoc + ' and will follow up.</p>';
    }
  }

  var html = '<p>Good morning,</p>'
    + '<p>Thank you for your request regarding ' + nameRef + dobPart + '.</p>'
    + '<p>' + (docCount > 0
        ? 'We have located the patient\'s chart. We have ' + docCount + ' document(s) on file for this patient.'
        : 'We are locating the patient\'s chart and will follow up shortly.')
    + '</p>'
    + specificDocHtml
    + '<p>' + (reqType === 'Billing/Insurance'
        ? 'Our billing team will follow up regarding this account.'
        : 'We will release the requested medical records upon confirmation'
          + ' that a valid signed authorization is on file.')
    + '</p>'
    + '<p>Please let us know if anything further is needed.</p>'
    + '<p>Atlantic Pain &amp; Wellness Institute<br>Medical Records</p>';

  return { plain: plain, html: html };
}

/**
 * Route a non-referral email that is still about a real patient
 * (attorney records request, insurance/claim update, billing letter).
 * Files PDFs into existing folder if found; always logs to the
 * "Records & Billing Requests" action-queue tab; never creates a new folder.
 *
 * @param {GmailApp.GmailMessage} msg
 * @param {GmailApp.GmailThread}  thread
 * @param {Object}                parsed     - output of parsePatientInfoFromText_
 * @param {string}                subject
 * @param {string}                body
 * @param {Object}                cfg
 * @param {SpreadsheetApp.Sheet}  ledgerSheet - the Runs sheet
 * @param {number}                claimRow
 * @param {string[]}              bulletin
 * @param {string}                reason     - classification.reason from Gemini
 */
function routeRecordsBillingRequest_(msg, thread, parsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, reason) {
  const patientName = (capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName)).trim();
  const requester   = msg.getFrom();
  var folder = (parsed.firstName && parsed.lastName)
    ? findExistingPatientFolder_(parsed.firstName, parsed.lastName, cfg, parsed.dob || '')
    : null;
  if (!folder) folder = findPatientFolderByLastName_(parsed.lastName, cfg);
  const folderUrl   = folder ? folder.getUrl() : '';

  // File PDF and image attachments into existing folder (only when folder is found)
  if (folder) {
    const attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });
    for (const att of attachments) {
      const ct = (att.getContentType() || '').toLowerCase();
      if (isOcrableAttachment_(ct)) {
        try {
          filePdfToFolder_(att.copyBlob(), att.getName(), folder, bulletin);
        } catch (e) {
          Logger.log(`routeRecordsBillingRequest_: PDF filing error - ${e}`);
        }
      }
    }
  }

  // Determine a short request type
  const reqType = /bill|ledger|statement|balance|insurance|claim|eob|benefit/i.test(subject + ' ' + body)
    ? 'Billing/Insurance'
    : 'Records';

  // Log to the Records & Billing Requests action-queue tab
  try {
    const ss  = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
    const tab = getOrCreateTab_(ss, 'Records & Billing Requests',
      ['Date', 'Patient', 'DOB', 'Requester', 'Subject', 'Request Type', 'Folder', 'Status', 'MessageId']);
    const folderFormula = folderUrl ? `=HYPERLINK("${folderUrl}","Open Folder")` : '';
    tab.appendRow([
      new Date().toISOString(),
      patientName,
      parsed.dob || '',
      requester,
      subject,
      reqType,
      folderFormula,
      'NEW',
      msg.getId()
    ]);
  } catch (e) {
    Logger.log(`routeRecordsBillingRequest_: tab logging error - ${e}`);
  }

  // Update main ledger row
  ledgerUpdate_(ledgerSheet, claimRow, 'records', patientName, parsed.confidence || '', folderUrl,
    `records/billing request: ${reason || ''}`.trim());

  // Apply label
  applyLabel_(thread, cfg.RECORDS_LABEL);
  try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}

  bulletin.push(`[REC] Records/billing request - ${patientName || '(name)'} from ${requester} -> queue${folder ? ' (filed to folder)' : ''}.`);

  // Count documents in the patient folder (CHANGE C)
  var docCount = 0;
  if (folder) {
    try {
      const it = folder.getFiles();
      while (it.hasNext() && docCount < 100) { it.next(); docCount++; }
    } catch (e) {
      Logger.log('routeRecordsBillingRequest_: file count error - ' + e);
    }
  }

  // Identify the specifically requested document (e.g., EMG) and whether it is on file.
  var reqDocMatch = (subject + ' ' + body).match(/\b(emg|ncs|nerve conduction|mri|x-?ray|ct scan|ultrasound|operative (?:report|note)|op note|ledger|itemized (?:bill|statement)|imaging|pathology|lab results?)\b/i);
  var requestedDoc = reqDocMatch ? reqDocMatch[1] : '';
  var requestedDocOnFile = false;
  if (folder && requestedDoc) {
    try {
      var fit = folder.getFiles();
      var needle2 = requestedDoc.toLowerCase().replace(/[^a-z]/g, '');
      while (fit.hasNext()) {
        var fname = fit.next().getName().toLowerCase().replace(/[^a-z]/g, '');
        if (needle2 && fname.indexOf(needle2) !== -1) { requestedDocOnFile = true; break; }
      }
    } catch (e) { Logger.log('requestedDoc scan: ' + e); }
  }

  // Auto-draft acknowledgment reply for staff to verify -- NEVER auto-sent (CHANGE C)
  if (cfg.DRAFT_RECORDS_REPLIES) {
    try {
      const reply = buildRecordsReplyBody_(patientName, parsed.dob || '', reqType, docCount, requestedDoc, requestedDocOnFile);
      thread.createDraftReply(reply.plain, { htmlBody: reply.html });
      bulletin.push('[REC] Reply DRAFT created for ' + (patientName || '(name)') + ' - staff: verify authorization, attach records from the folder, then send.');
    } catch (e) {
      Logger.log('routeRecordsBillingRequest_: draft reply error - ' + e);
      bulletin.push('[WARN] Could not draft records reply for ' + (patientName || '(name)') + ': ' + e);
    }
  }
}

// ---------------------------------------------------------------------------
// # Booking-request routing lane
// ---------------------------------------------------------------------------

/**
 * Route a scheduling/booking request to the "Booking Requests" action-queue
 * tab, file any OCRable attachments to the patient's existing folder when
 * findable, apply BOOKING_LABEL, and send the instant "call to schedule"
 * alert. Modeled closely on routeRecordsBillingRequest_. Never auto-sends
 * anything to the patient/requester - only an internal NOTIFY_EMAIL alert.
 */
function routeBookingRequest_(msg, thread, parsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, reason) {
  const patientName = (capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName)).trim();
  const requester   = msg.getFrom();
  var folder = (parsed.firstName && parsed.lastName)
    ? findExistingPatientFolder_(parsed.firstName, parsed.lastName, cfg, parsed.dob || '')
    : null;
  if (!folder && parsed.lastName) folder = findPatientFolderByLastName_(parsed.lastName, cfg);
  const folderUrl = folder ? folder.getUrl() : '';

  // File PDF and image attachments into existing folder (only when folder is found)
  if (folder) {
    const attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });
    for (const att of attachments) {
      const ct = (att.getContentType() || '').toLowerCase();
      if (isOcrableAttachment_(ct)) {
        try {
          filePdfToFolder_(att.copyBlob(), att.getName(), folder, bulletin);
        } catch (e) {
          Logger.log(`routeBookingRequest_: PDF filing error - ${e}`);
        }
      }
    }
  }

  // Extract a phone number via a simple pattern match on the body; '' if none.
  const phoneMatch = String(body || '').match(/\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}/);
  const phone = phoneMatch ? phoneMatch[0] : '';

  // Log to the Booking Requests action-queue tab
  try {
    const ss  = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
    const tab = getOrCreateTab_(ss, 'Booking Requests',
      ['Date', 'Patient', 'DOB', 'Requester', 'Phone', 'Subject', 'Folder', 'Status', 'MessageId']);
    const folderFormula = folderUrl ? `=HYPERLINK("${folderUrl}","Open Folder")` : '';
    tab.appendRow([
      new Date().toISOString(),
      patientName,
      parsed.dob || '',
      requester,
      phone,
      subject,
      folderFormula,
      'NEW',
      msg.getId()
    ]);
  } catch (e) {
    Logger.log(`routeBookingRequest_: tab logging error - ${e}`);
  }

  // Update main ledger row
  ledgerUpdate_(ledgerSheet, claimRow, 'booking', patientName, parsed.confidence || '', folderUrl,
    `booking request: ${reason || ''}`.trim());

  // Apply label
  applyLabel_(thread, cfg.BOOKING_LABEL);
  try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}

  bulletin.push(`[BOOK] Booking request - ${patientName || subject} -> queue + alert.`);

  // Instant alert to staff ("call to schedule"). Never fails the run.
  if (cfg.NOTIFY_ON_REFERRAL) {
    try {
      var alertMsg = buildBookingAlertBody_(patientName, requester, phone, folderUrl, subject);
      var alertTo = cfg.NOTIFY_EMAIL || Session.getEffectiveUser().getEmail();
      GmailApp.sendEmail(alertTo, alertMsg.subject, alertMsg.plain, { htmlBody: alertMsg.html });
      bulletin.push(`[ALERT] Booking alert sent - ${patientName || subject} -> ${alertTo}`);
    } catch (e) {
      Logger.log(`routeBookingRequest_: booking alert failed: ${e}`);
      bulletin.push(`[WARN] Booking alert failed: ${e}`);
    }
  }
}

// ---------------------------------------------------------------------------
// # Thread follow-up routing (already-labeled patient threads)
// ---------------------------------------------------------------------------

/**
 * Log a new message that arrived on an already-labeled ("prior lane") patient
 * thread so it always ends with a visible, tracked outcome instead of being
 * classified in isolation or silently ignored (the Damon Holden gap: three
 * inbound nudges on an attorney thread ended with no label and no outcome).
 * Files OCRable attachments to the patient's existing folder when findable
 * by parsed name (skipped silently when not found - never creates a folder).
 * Sends the same guarded instant-alert pattern used elsewhere in the file.
 */
function routeThreadFollowup_(msg, thread, parsed, subject, cfg, ledgerSheet, claimRow, bulletin, priorLane) {
  const sender = msg.getFrom();

  // File OCRable attachments to the existing patient folder when findable; skip silently otherwise.
  try {
    if (parsed && parsed.firstName && parsed.lastName) {
      var folder = findExistingPatientFolder_(parsed.firstName, parsed.lastName, cfg, parsed.dob || '');
      if (!folder && parsed.lastName) folder = findPatientFolderByLastName_(parsed.lastName, cfg);
      if (folder) {
        var attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });
        for (var i = 0; i < attachments.length; i++) {
          var ct = (attachments[i].getContentType() || '').toLowerCase();
          if (isOcrableAttachment_(ct)) {
            try {
              filePdfToFolder_(attachments[i].copyBlob(), attachments[i].getName(), folder, bulletin);
            } catch (e) {
              Logger.log(`routeThreadFollowup_: PDF filing error - ${e}`);
            }
          }
        }
      }
    }
  } catch (e) {
    Logger.log(`routeThreadFollowup_: folder lookup failed (non-fatal) - ${e}`);
  }

  // Log to the Follow-Ups tab
  try {
    const ss  = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
    const tab = getOrCreateTab_(ss, 'Follow-Ups', ['Date', 'Subject', 'From', 'Prior Lane', 'Status', 'MessageId']);
    tab.appendRow([new Date().toISOString(), subject, sender, priorLane, 'NEW', msg.getId()]);
  } catch (e) {
    Logger.log(`routeThreadFollowup_: tab logging error - ${e}`);
  }

  ledgerUpdate_(ledgerSheet, claimRow, 'followup', (parsed && (parsed.firstName || parsed.lastName))
    ? (capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName)).trim() : '',
    parsed ? (parsed.confidence || '') : '', '', `follow-up on ${priorLane} thread`);

  bulletin.push(`[FUP] Follow-up on ${priorLane} thread - "${subject}"`);

  // Instant alert to staff. Never fails the run.
  if (cfg.NOTIFY_ON_REFERRAL) {
    try {
      var alertSubject = `[Intake Alert] Follow-up (${priorLane}): ${subject}`;
      var alertBody = `A new message arrived on a ${priorLane} thread: ${subject} from ${sender}. Action: open the thread and respond.`;
      var alertTo = cfg.NOTIFY_EMAIL || Session.getEffectiveUser().getEmail();
      GmailApp.sendEmail(alertTo, alertSubject, alertBody);
      bulletin.push(`[ALERT] Follow-up alert sent - "${subject}" -> ${alertTo}`);
    } catch (e) {
      Logger.log(`routeThreadFollowup_: follow-up alert failed: ${e}`);
      bulletin.push(`[WARN] Follow-up alert failed: ${e}`);
    }
  }
}

// ---------------------------------------------------------------------------
// # Gate 1 - deterministic filters
// ---------------------------------------------------------------------------

/**
 * True when a subject line is a self-notification sent BY this system (the
 * instant new-referral alert from fullProcess_). Used as a loop guard: the
 * alert may land in the same inbox the system scans, and must never be
 * mistaken for a new referral. Allows a leading Re:/Fwd: and whitespace.
 * Pure function - unit-testable in Node.
 * @param {string} s
 * @returns {boolean}
 */
function isIntakeAlertSubject_(s) {
  if (typeof s !== 'string') return false;
  var stripped = s.replace(/^\s*((re|fwd?)\s*:\s*)+/i, '');
  return /^\s*\[Intake( Alert)?\]/.test(stripped);
}

/**
 * Parse a RingCentral "New Fax Message from ... on ..." notification email.
 * The fax document is NOT attached to these notifications, so callers must
 * queue the fax for human triage rather than ignore or auto-file it.
 * Returns null when the subject does not match. Pure function - unit-testable
 * in Node.
 * @param {string} subject
 * @param {string} body
 * @returns {{fromNumber:string, pages:number}|null}
 */
function parseFaxNotification_(subject, body) {
  var subjectRe = /^New Fax Message from (.+) on /i;
  var m = subjectRe.exec(String(subject || ''));
  if (!m) return null;
  var pagesRe = /Pages:\s*(\d+)/i;
  var pagesMatch = pagesRe.exec(String(body || ''));
  return {
    fromNumber: m[1].trim(),
    pages: pagesMatch ? parseInt(pagesMatch[1], 10) : 0
  };
}

/**
 * Build the plain-text and HTML bodies for the instant "new referral -
 * call to book" staff alert. Pure function - no Apps Script globals - so
 * it is unit-testable in Node. Never invents missing fields; renders '-'.
 *
 * @param {string}  nameFull      - Full patient name, e.g. "Jose Cartagena"
 * @param {string}  dob           - Formatted DOB string or empty string
 * @param {string}  caseType      - e.g. "Workers Comp", "MVA", or '' if unknown
 * @param {boolean} isHeadInjury  - whether head-injury markers were detected
 * @param {string}  phone         - patient phone or empty string
 * @param {string}  referredBy    - referring provider/source or empty string
 * @param {string}  folderUrl     - Drive folder URL for the new patient
 * @returns {{ subject: string, plain: string, html: string }}
 */
function buildReferralAlertBody_(nameFull, dob, caseType, isHeadInjury, phone, referredBy, folderUrl) {
  var name       = nameFull || '[FILL IN]';
  var dobPart    = dob || '—';
  var casePart   = caseType || '—';
  var phonePart  = phone || '—';
  var referrerPart = referredBy || '—';
  var folderPart = folderUrl || '—';
  var headFlag   = isHeadInjury ? ' (HEAD INJURY)' : '';

  var subject = '[Intake Alert] New referral: ' + name + ' — call to book';

  var plain = 'New referral received - call the patient now to schedule their first appointment.\n\n'
    + 'Patient: ' + name + '\n'
    + 'DOB: ' + dobPart + '\n'
    + 'Case type: ' + casePart + headFlag + '\n'
    + 'Phone: ' + phonePart + '\n'
    + 'Referred by: ' + referrerPart + '\n'
    + 'Drive folder: ' + folderPart + '\n\n'
    + 'Call the patient now to schedule their first appointment.';

  var html = '<p><strong>New referral received</strong> - call the patient now to schedule their first appointment.</p>'
    + '<p>'
    + 'Patient: ' + name + '<br>'
    + 'DOB: ' + dobPart + '<br>'
    + 'Case type: ' + casePart + headFlag + '<br>'
    + 'Phone: ' + phonePart + '<br>'
    + 'Referred by: ' + referrerPart + '<br>'
    + 'Drive folder: ' + folderPart
    + '</p>'
    + '<p><strong>Call the patient now to schedule their first appointment.</strong></p>';

  return { subject: subject, plain: plain, html: html };
}

/**
 * Returns true if the message carries bulk/marketing headers.
 * @param {GmailApp.GmailMessage} message
 * @returns {boolean}
 */
function isBulkMail_(message) {
  try {
    const raw = message.getRawContent();
    return /^list-unsubscribe\s*:/mi.test(raw) ||
           /^precedence\s*:\s*(bulk|list|junk)/mi.test(raw);
  } catch (_) {
    return false;
  }
}

/**
 * True when the lowercased from-header contains any comma-separated entry
 * from csvList (trimmed, empties skipped). Config-driven, deterministic
 * sender-ignore rule. Pure function - unit-testable in Node.
 * @param {string} from     - message "From" header (any case/format)
 * @param {string} csvList  - comma-separated list of sender strings to ignore
 * @returns {boolean}
 */
function isIgnoredSender_(from, csvList) {
  const fromLower = String(from || '').toLowerCase();
  if (!fromLower) return false;
  const entries = String(csvList || '').split(',').map(s => s.trim()).filter(Boolean);
  return entries.some(entry => fromLower.indexOf(entry) !== -1);
}

/**
 * True when the from-header's @domain ends with one of the listed partner
 * billing domains (subdomains match too, e.g. "billing.mdmanage.com").
 * Pure function - unit-testable in Node.
 * @param {string} from        - message "From" header (any case/format)
 * @param {string} csvDomains  - comma-separated list of domains
 * @returns {boolean}
 */
function isPartnerBillingSender_(from, csvDomains) {
  const fromLower = String(from || '').toLowerCase();
  const atIdx = fromLower.lastIndexOf('@');
  if (atIdx === -1) return false;
  // Strip any trailing ">" or similar from "Name <user@domain.com>" headers.
  const domainPart = fromLower.slice(atIdx + 1).replace(/[^a-z0-9.\-]+.*$/i, '');
  const domains = String(csvDomains || '').split(',').map(s => s.trim()).filter(Boolean);
  return domains.some(domain => domainPart === domain || domainPart.endsWith('.' + domain));
}

/**
 * Returns true if the email looks like a vendor invoice/billing notice
 * AND lacks referral evidence. BOTH conditions required.
 * @param {string} subject
 * @param {string} body
 * @returns {boolean}
 */
function isVendorInvoice_(subject, body) {
  const invoicePattern = /\b(invoice|past due|payment (due|reminder|received|failed)|billing statement|receipt for|renewal|subscription|account (suspended|overdue|notice))\b/i;
  const referralPattern = /\b(referr(al|ed|ing)|date of birth|DOB|injur|MRI|X-?ray|therapy|work(ers)? comp|accident|new patient)\b/i;
  const invoiceHit = invoicePattern.test(subject);
  const referralHit = referralPattern.test(subject + ' ' + body.slice(0, 4000));
  return invoiceHit && !referralHit;
}

/**
 * True when the lowercased from-header contains any comma-separated address
 * from csvAddresses (trimmed, empties skipped). Used to distinguish the
 * clinic's own outbound replies on an already-handled thread from a genuine
 * inbound follow-up. Pure function - unit-testable in Node.
 * @param {string} from          - message "From" header (any case/format)
 * @param {string} csvAddresses  - comma-separated list of the clinic's own addresses
 * @returns {boolean}
 */
function isOwnAddress_(from, csvAddresses) {
  const fromLower = String(from || '').toLowerCase();
  if (!fromLower) return false;
  const addrs = String(csvAddresses || '').split(',').map(s => s.trim()).filter(Boolean);
  return addrs.some(addr => fromLower.indexOf(addr) !== -1);
}

/**
 * True when the subject/body reads like a patient (or their representative)
 * asking to schedule/reschedule an appointment, AND the text does NOT match
 * the records/billing keyword set (records/billing requests keep priority
 * over the booking lane - see isRecordsOrBillingRequest_). Pure function -
 * unit-testable in Node.
 * @param {string} subject
 * @param {string} body
 * @returns {boolean}
 */
function isBookingRequest_(subject, body) {
  const text = (String(subject || '') + '\n' + String(body || ''));
  const bookingPattern = /\b(appointment|appt|schedul(e|ing)|re-?schedul(e|ing)|book(ing)?\s+(him|her|them|an appointment)|REQ\s*FOR\s*APPOINTMENT)\b/i;
  if (!bookingPattern.test(text)) return false;
  // Duplicated keyword set from isRecordsOrBillingRequest_ - records/billing requests
  // keep priority even when the same email also mentions scheduling.
  const lower = text.toLowerCase();
  const recordsBillingPatterns = [
    /\brecords?\s+request\b/,
    /\brequest\s+for\s+records?\b/,
    /\bmedical\s+records?\b/,
    /\bsend\s+(the\s+)?records?\b/,
    /\bcopy\s+of\s+(the\s+)?records?\b/,
    /\brelease\s+of\s+records?\b/,
    /\brecords?\s+release\b/,
    /\bauthorization\s+to\s+release\b/,
    /\bnarrative\s+report\b/,
    /\bletter\s+of\s+protection\b/,
    /\blop\b/,
    /\blien\b/,
    /\bsubpoena\b/,
    /\bdeposition\b/,
    /\bbilling\b/,
    /\bitemized\s+(bill|statement|ledger)\b/,
    /\bbill(ing)?\s+statement\b/,
    /\bledger\b/,
    /\bbalance\s+due\b/,
    /\boutstanding\s+balance\b/,
    /\baccount\s+statement\b/,
    /\binsurance\b/,
    /\bclaim\s+(number|#)\b/,
    /\bclaim\s*#/,
    /\bnon-?par\b/,
    /\beob\b/,
    /\bexplanation\s+of\s+benefits\b/,
    /\bpre-?authorization\b/,
    /\bverification\s+of\s+benefits\b/,
    /\bforward\s+(me\s+)?(a\s+)?copy\s+of\b/,
    /\bcopy\s+of\s+.*\b(report|records?|emg|mri|x-?ray|ct|results?|chart|note|imaging)\b/,
    /\bsend\s+(me\s+)?.*\b(report|records?|emg|mri|x-?ray|results?|chart|bill|ledger|itemized)\b/,
    /\brequest(ing)?\s+.*\b(report|records?|emg|mri|results?|chart)\b/,
    /\bneed\s+.*\b(report|records?|emg|mri|results?|chart|bill|ledger)\b/,
    /\b(emg|ncs|mri|x-?ray|ct)\s+(report|results?)\b/
  ];
  if (recordsBillingPatterns.some(re => re.test(lower))) return false;
  return true;
}

/**
 * Build the plain-text and HTML bodies for the instant "booking request"
 * alert email. Pure function - mirrors buildReferralAlertBody_'s style -
 * unit-testable in Node.
 * @param {string} nameFull   - "First Last" (or "" if unknown)
 * @param {string} requester  - the From header of the request
 * @param {string} phone      - phone number extracted from the body, or ""
 * @param {string} folderUrl  - existing patient folder URL, or ""
 * @param {string} subject    - original email subject (fallback label)
 * @returns {{subject: string, plain: string, html: string}}
 */
function buildBookingAlertBody_(nameFull, requester, phone, folderUrl, subject) {
  var name         = (nameFull && nameFull.trim()) || (subject || '—');
  var requesterPart = requester || '—';
  var phonePart     = phone || '—';
  var folderPart    = folderUrl || '—';

  var alertSubject = '[Intake Alert] Booking request: ' + name;

  var plain = 'A patient (or their representative) asked to schedule an appointment.\n\n'
    + 'Patient: ' + name + '\n'
    + 'Requested by: ' + requesterPart + '\n'
    + 'Phone: ' + phonePart + '\n'
    + 'Folder: ' + folderPart + '\n\n'
    + 'Call to schedule this appointment now.';

  var html = '<p><strong>A patient (or their representative) asked to schedule an appointment.</strong></p>'
    + '<p>'
    + 'Patient: ' + name + '<br>'
    + 'Requested by: ' + requesterPart + '<br>'
    + 'Phone: ' + phonePart + '<br>'
    + 'Folder: ' + folderPart
    + '</p>'
    + '<p><strong>Call to schedule this appointment now.</strong></p>';

  return { subject: alertSubject, plain: plain, html: html };
}

/**
 * True if the email is a B2B sales/marketing solicitation to the practice (not patient-related).
 * Requires a sales signal AND the absence of any patient/referral/records signal. Pure.
 */
function isSalesSolicitation_(subject, body) {
  var text = ((subject || '') + '\n' + (body || '')).toLowerCase();
  // Never treat a patient/referral/records email as sales.
  var patientSignal = /\b(referr(al|ed|ing)|patient|date of birth|\bdob\b|injur|mri|emg|x-?ray|workers?\s*comp|accident|medical records?|records? request|itemized|ledger|lien|subpoena|letter of protection|claim\s*(number|#))\b/i;
  if (patientSignal.test(text)) return false;
  var salesSignal = [
    /\b401\(?k\)?\b/, /\bretirement\s+(plan|services|savings)\b/, /\bpre-?tax\s+deduction/,
    /\bpayroll\b/, /\bpre-?tax\b/, /\bprofit\s+sharing\b/,
    /\blet'?s\s+(chat|connect|schedule|hop\s+on)/, /\bbook\s+(a\s+)?(call|demo|meeting)/,
    /\bschedule\s+(a\s+)?(call|demo|quick\s+call)/, /\bquick\s+(call|chat)\b/,
    /\bpromotion(s)?\b/, /\bwaived?\s+fees?\b/, /\bsetup\s+fees?\b/, /\blimited[-\s]time\b/,
    /\bspecial\s+offer\b/, /\bfree\s+(consultation|trial|demo)\b/, /\btax\s+credit(s)?\b/,
    /\bour\s+(services|solution|platform|product)\b/, /\breach(ing)?\s+out\b/,
    /\bpartner\s+with\b/, /\bgrow\s+your\s+(business|practice)\b/
  ];
  var hits = 0;
  for (var i = 0; i < salesSignal.length; i++) { if (salesSignal[i].test(text)) hits++; }
  return hits >= 2;  // require 2+ sales cues to avoid false positives
}

/**
 * True if a non-referral email is still a patient records / billing / insurance request. Pure.
 * Runs ONLY after Gemini has already returned isReferral=false.
 * @param {string} subject
 * @param {string} body
 * @returns {boolean}
 */
function isRecordsOrBillingRequest_(subject, body) {
  const text = (subject + '\n' + body).toLowerCase();
  const patterns = [
    // Records signals
    /\brecords?\s+request\b/,
    /\brequest\s+for\s+records?\b/,
    /\bmedical\s+records?\b/,
    /\bsend\s+(the\s+)?records?\b/,
    /\bcopy\s+of\s+(the\s+)?records?\b/,
    /\brelease\s+of\s+records?\b/,
    /\brecords?\s+release\b/,
    /\bauthorization\s+to\s+release\b/,
    /\bnarrative\s+report\b/,
    /\bletter\s+of\s+protection\b/,
    /\blop\b/,
    /\blien\b/,
    /\bsubpoena\b/,
    /\bdeposition\b/,
    // Billing signals
    /\bbilling\b/,
    /\bitemized\s+(bill|statement|ledger)\b/,
    /\bbill(ing)?\s+statement\b/,
    /\bledger\b/,
    /\bbalance\s+due\b/,
    /\boutstanding\s+balance\b/,
    /\baccount\s+statement\b/,
    // Insurance / claim signals
    /\binsurance\b/,
    /\bclaim\s+(number|#)\b/,
    /\bclaim\s*#/,
    /\bnon-?par\b/,
    /\beob\b/,
    /\bexplanation\s+of\s+benefits\b/,
    /\bpre-?authorization\b/,
    /\bverification\s+of\s+benefits\b/,
    // Broad request phrasing (B1)
    /\bforward\s+(me\s+)?(a\s+)?copy\s+of\b/,
    /\bcopy\s+of\s+.*\b(report|records?|emg|mri|x-?ray|ct|results?|chart|note|imaging)\b/,
    /\bsend\s+(me\s+)?.*\b(report|records?|emg|mri|x-?ray|results?|chart|bill|ledger|itemized)\b/,
    /\brequest(ing)?\s+.*\b(report|records?|emg|mri|results?|chart)\b/,
    /\bneed\s+.*\b(report|records?|emg|mri|results?|chart|bill|ledger)\b/,
    /\b(emg|ncs|mri|x-?ray|ct)\s+(report|results?)\b/
  ];
  return patterns.some(re => re.test(text));
}

/**
 * Returns true if the email looks like an inbound clinical report/result for an existing patient.
 * @param {string} subject
 * @param {string} body
 * @returns {boolean}
 */
function isInboundPatientReport_(subject, body) {
  const text = (subject + '\n' + body).toLowerCase();
  const patterns = [
    // Electrophysiology / nerve studies
    /\bemg\b/,
    /\beeg\b/,
    /\bncs\b/,
    /\bnerve\s+conduction\b/,
    // Imaging studies
    /\bmri\b/,
    /\bct\s+scan\b/,
    /\bct\s+/,
    /\bx-?ray\b/,
    /\bxray\b/,
    /\bultrasound\b/,
    /\bimaging\b/,
    /\bradiology\b/,
    // Lab / pathology
    /\bpathology\b/,
    /\blab\s+results?\b/,
    /\blaboratory\b/,
    // Operative / surgical
    /\boperative\s+(?:report|note)\b/,
    /\bop\s+note\b/,
    /\bdischarge\s+summary\b/,
    // Consult / progress / H&P
    /\bconsult(?:ation)?\s+(?:report|note)\b/,
    /\bprogress\s+note\b/,
    /\bh\s*&\s*p\b/,
    /\bhistory\s+and\s+physical\b/,
    // Findings / results language
    /\btest\s+results?\b/,
    /\bresults?\s+(?:enclosed|attached|for)\b/,
    /\bfindings?\b/,
    /\bimpression\b/,
    // Diagnostic report
    /\bdiagnostic\s+(?:report|study|results?)\b/,
    // Generic clinical document labels
    /\bmedical\s+report\b/,
    /\bclinical\s+(?:report|note|summary)\b/,
    /\breport\s+(?:attached|enclosed)\b/
  ];
  return patterns.some(re => re.test(text));
}

/**
 * Route an inbound clinical report/result to the patient's existing folder,
 * or quarantine if no folder is found.
 */
function routeInboundReport_(msg, thread, parsed, subject, body, cfg, ledgerSheet, claimRow, bulletin, precomputedText) {
  try {
    const patientName = (capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName)).trim();
    const folder      = findExistingPatientFolder_(parsed.firstName, parsed.lastName, cfg, parsed.dob || '');
    let folderUrl     = '';

    // Multi-patient document guard: a document that labels 2+ distinct patient DOBs
    // is a multi-patient packet (e.g. a batched op-note PDF). Never auto-file it to
    // a single patient's folder - the other patients' pages would never reach their
    // charts. Route to review so a human can split and file manually.
    const distinctDobCount = countDistinctDobs_(precomputedText || body);
    if (distinctDobCount >= 2) {
      ledgerUpdate_(ledgerSheet, claimRow, 'review', patientName, parsed.confidence || '', '',
        `multi-patient document (${distinctDobCount} distinct DOBs) - split and file manually`);
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push(`[WARN] Multi-patient document detected (${distinctDobCount} distinct DOBs) in "` + subject + '" - split and file manually.');
      return;
    }

    if (folder) {
      folderUrl = folder.getUrl();
      // File each PDF and image attachment into the folder
      const attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });
      for (const att of attachments) {
        const ct = (att.getContentType() || '').toLowerCase();
        if (isOcrableAttachment_(ct)) {
          try {
            filePdfToFolder_(att.copyBlob(), att.getName(), folder, bulletin);
          } catch (e) {
            Logger.log('routeInboundReport_: PDF filing error - ' + e);
          }
        }
      }
      ledgerUpdate_(ledgerSheet, claimRow, 'report-filed', patientName, parsed.confidence || '', folderUrl, 'inbound patient report filed to folder');
      applyLabel_(thread, cfg.REPORTS_LABEL);
      try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
      bulletin.push('[REC] Inbound report filed to folder for ' + patientName + '.');
    } else {
      quarantinePdfs_(msg, cfg);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', patientName, parsed.confidence || '', '', 'inbound patient report but no existing folder; PDFs quarantined');
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Inbound report for ' + patientName + ' - no patient folder found; quarantined for review.');
    }

    // Log to Inbound Reports tab
    const ss  = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
    const tab = getOrCreateTab_(ss, 'Inbound Reports', ['Date', 'Patient', 'DOB', 'Subject', 'Folder', 'Status', 'MessageId']);
    const folderFormula = folderUrl ? '=HYPERLINK("' + folderUrl + '","Open Folder")' : '';
    tab.appendRow([new Date().toISOString(), patientName, parsed.dob || '', subject, folderFormula, folder ? 'FILED' : 'NEEDS FOLDER', msg.getId()]);
  } catch (e) {
    Logger.log('[WARN] inbound report step failed: ' + e);
    const patientName = (parsed && (parsed.firstName || parsed.lastName))
      ? (capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName)).trim()
      : '';
    try {
      ledgerUpdate_(ledgerSheet, claimRow, 'review', patientName, parsed ? (parsed.confidence || '') : '', '',
        'inbound patient report routing failed; manual review required: ' + e);
    } catch (_) {}
    try { applyLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    bulletin.push('[WARN] inbound report step failed: ' + e);
    return;
  }
}

// ---------------------------------------------------------------------------
// # Gate 2 - identity parsing
// ---------------------------------------------------------------------------

// Name token characters: letters, accented, hyphens, apostrophes
const NAME_CHAR = "[A-Za-z---'\\-]";
const NAME_TOKEN = `${NAME_CHAR}+`;
// Optional middle name + optional suffix
const SUFFIX_PAT = '(?:\\s+(?:Jr\\.?|Sr\\.?|II|III|IV))?';
const MID_PAT    = `(?:\\s+${NAME_TOKEN})?`;

/**
 * Build salutation-exclusion check: is the preceding text a salutation?
 * Returns true if the line up to the match ends with a salutation word.
 * @param {string} fullText
 * @param {number} matchIndex  start index of the name in fullText
 */
function hasSalutation_(fullText, matchIndex) {
  // Get the portion of the current line BEFORE the match
  const lineStart = fullText.lastIndexOf('\n', matchIndex - 1) + 1;
  const before    = fullText.slice(lineStart, matchIndex);
  return /\b(dear|hi|hello|dr\.?|attn:?)\s*$/i.test(before);
}

/**
 * Guard against OCR/parser noise being mistaken for a real patient name
 * (e.g. "Please Hello" pulled from boilerplate PDF text). Pure.
 * @param {string} first
 * @param {string} last
 * @returns {boolean} true when either token is empty, a single letter, or a
 *   common non-name word (greeting/boilerplate/PHI-field-label).
 */
function isImplausibleName_(first, last) {
  const NAME_BLOCKLIST = new Set([
    'please', 'hello', 'dear', 'thanks', 'thank', 'regards', 'sincerely',
    'hi', 'hey', 'patient', 'patients', 'records', 'record', 'referral',
    'referrals', 'report', 'reports', 'attn', 'attention', 'office',
    'doctor', 'test', 'sample', 'none', 'unknown', 'medical', 'billing',
    'fax', 'email', 'date', 'birth', 'name', 'provider', 'clinic',
    'request', 'urgent', 'confidential', 'attached', 'review', 'notes',
    'summary', 'insurance', 'claim', 'invoice', 'statement', 'dob',
    'gender', 'male', 'female', 'address', 'phone'
  ]);
  const f = String(first || '').trim().toLowerCase();
  const l = String(last  || '').trim().toLowerCase();
  if (!f || !l) return true;
  if (f.length === 1 || l.length === 1) return true;
  if (NAME_BLOCKLIST.has(f) || NAME_BLOCKLIST.has(l)) return true;
  return false;
}

/**
 * Parse patient name and labeled DOB from email text.
 * @param {string} subject
 * @param {string} text  full plain-body text
 * @returns {{ firstName:string, lastName:string, dob:string, confidence:string, dobSource:string }}
 */
function parsePatientInfoFromText_(subject, text) {
  const combined = subject + '\n' + text;

  let firstName = '', lastName = '', dob = '', dobSource = '';

  // --- DOB (labeled only) ---
  const dobLabelRe = /\b(?:dob|d\.o\.b\.?|date of birth|birth\s?date)\b[:\s]*/gi;
  const dateRe = /(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|\d{4}[\/\-]\d{2}[\/\-]\d{2})/i;
  let dobMatch = null;
  let m;
  const dobLabelReCopy = new RegExp(dobLabelRe.source, 'gi');
  while ((m = dobLabelReCopy.exec(combined)) !== null) {
    const after = combined.slice(m.index + m[0].length, m.index + m[0].length + 30);
    const dm = dateRe.exec(after);
    if (dm) {
      const formatted = formatDob_(dm[1]);
      if (formatted) { dobMatch = formatted; dobSource = 'labeled'; break; }
    }
  }
  dob = dobMatch || '';

  // --- Name patterns (in priority order) ---

  // 1. "Patient: Last, First [Middle] [Suffix]"
  const patLastFirst = new RegExp(
    `\\bpatient\\s*[:\\-]\\s*(${NAME_TOKEN}),\\s*(${NAME_TOKEN})${MID_PAT}${SUFFIX_PAT}`, 'i'
  );
  let nm = patLastFirst.exec(combined);
  if (nm && !hasSalutation_(combined, nm.index)) {
    lastName  = nm[1];
    firstName = nm[2];
  }

  // 2. "Patient Name: First [Middle] Last [Suffix]"
  if (!firstName) {
    const patNameRe = new RegExp(
      `\\bpatient\\s+name\\s*[:\\-]\\s*(${NAME_TOKEN})${MID_PAT}\\s+(${NAME_TOKEN})${SUFFIX_PAT}`, 'i'
    );
    nm = patNameRe.exec(combined);
    if (nm && !hasSalutation_(combined, nm.index)) {
      firstName = nm[1];
      lastName  = nm[nm.length - 2] || nm[2];  // last captured group before suffix
    }
  }

  // 3. "RE: First Last" (subject line pattern)
  if (!firstName) {
    const reRe = new RegExp(`\\bRE:\\s*(${NAME_TOKEN})\\s+(${NAME_TOKEN})${SUFFIX_PAT}`, 'i');
    nm = reRe.exec(subject);
    if (nm && !hasSalutation_(subject, nm.index)) {
      firstName = nm[1];
      lastName  = nm[2];
    }
  }

  // 4. "Last, First [Middle]" near a DOB label (within 200 chars)
  if (!firstName && dob) {
    const lastFirstRe = new RegExp(`(${NAME_TOKEN}),\\s*(${NAME_TOKEN})${MID_PAT}`, 'g');
    let lfm;
    while ((lfm = lastFirstRe.exec(combined)) !== null) {
      // Check proximity to dob label
      const region = combined.slice(Math.max(0, lfm.index - 200), lfm.index + lfm[0].length + 200);
      if (/\b(?:dob|d\.o\.b\.?|date of birth|birth\s?date)\b/i.test(region) &&
          !hasSalutation_(combined, lfm.index)) {
        lastName  = lfm[1];
        firstName = lfm[2];
        break;
      }
    }
  }

  // 5. Plain "First Last" ONLY when within 40 chars of a DOB label
  if (!firstName && dob) {
    const plainRe = new RegExp(`(${NAME_TOKEN})\\s+(${NAME_TOKEN})`, 'g');
    let pm;
    while ((pm = plainRe.exec(combined)) !== null) {
      const start = pm.index;
      const window40Before = combined.slice(Math.max(0, start - 40), start);
      const window40After  = combined.slice(start + pm[0].length, start + pm[0].length + 40);
      const dobNearby = /\b(?:dob|d\.o\.b\.?|date of birth|birth\s?date)\b/i;
      if ((dobNearby.test(window40Before) || dobNearby.test(window40After)) &&
          !hasSalutation_(combined, start)) {
        // Sanity: reject common non-name tokens
        if (!/\b(the|and|or|to|from|re|patient|name|dear|dr)\b/i.test(pm[1]) &&
            !/\b(the|and|or|to|from|re|patient|name|dear|dr)\b/i.test(pm[2])) {
          firstName = pm[1];
          lastName  = pm[2];
          break;
        }
      }
    }
  }

  // Normalise
  firstName = capitalizeFirst_(firstName.trim());
  lastName  = capitalizeFirst_(lastName.trim());

  // Staff blocklist check
  if (firstName || lastName) {
    // load config to check blocklist - use PropertiesService directly so this function
    // remains pure (no cfg dependency) - but the caller may also check via cfg.
    const rawBl = (PropertiesService.getScriptProperties().getProperty('STAFF_NAME_BLOCKLIST') || '');
    const blocklist = rawBl.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    const fullNameLower = `${firstName} ${lastName}`.trim().toLowerCase();
    if (blocklist.some(bl => bl === fullNameLower)) {
      return { firstName, lastName, dob, confidence: 'blocked', dobSource };
    }
  }

  // Determine confidence
  let confidence = 'none';
  if (firstName && lastName && dob) confidence = 'high';
  else if (firstName && lastName)   confidence = 'low';

  if (confidence === 'high' && isImplausibleName_(firstName, lastName)) {
    Logger.log(`parsePatientInfoFromText_: implausible name blocked ("${firstName} ${lastName}"); downgrading confidence high -> low`);
    confidence = 'low';
  }

  return { firstName, lastName, dob, confidence, dobSource };
}

/**
 * Normalise a raw date string to MM-DD-YYYY.
 * - Two-digit year: >= (currentYear%100 + 1) -> 1900s; else 2000s.
 * - Rejects impossible dates and ages outside 0-110.
 * @param {string} raw
 * @returns {string}  MM-DD-YYYY or ''
 */
function formatDob_(raw) {
  if (!raw) return '';
  raw = raw.trim();

  let month = 0, day = 0, year = 0;

  // ISO: YYYY-MM-DD or YYYY/MM/DD
  const iso = /^(\d{4})[\/\-](\d{2})[\/\-](\d{2})$/.exec(raw);
  if (iso) { year = +iso[1]; month = +iso[2]; day = +iso[3]; }

  // Numeric: M/D/YY or M/D/YYYY or M-D-YY or M-D-YYYY
  if (!year) {
    const num = /^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})$/.exec(raw);
    if (num) { month = +num[1]; day = +num[2]; year = +num[3]; }
  }

  // Written: "Jan 1, 1990" or "January 1 1990"
  if (!year) {
    const mon = { jan:1, feb:2, mar:3, apr:4, may:5, jun:6, jul:7, aug:8, sep:9, oct:10, nov:11, dec:12 };
    const writ = /^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})$/i.exec(raw);
    if (writ) {
      month = mon[writ[1].toLowerCase()] || 0;
      day   = +writ[2];
      year  = +writ[3];
    }
  }

  if (!month || !day || !year) return '';

  // Two-digit year expansion
  if (year < 100) {
    const curTwoDigit = new Date().getFullYear() % 100;
    year = year >= (curTwoDigit + 1) ? 1900 + year : 2000 + year;
  }

  // Validate range
  if (month < 1 || month > 12 || day < 1 || day > 31) return '';
  try {
    const d = new Date(year, month - 1, day);
    if (d.getMonth() !== month - 1 || d.getDate() !== day) return ''; // overflow
  } catch (_) { return ''; }

  // Age check 0-110
  const ageMs = Date.now() - new Date(year, month - 1, day).getTime();
  const ageYears = ageMs / (1000 * 60 * 60 * 24 * 365.25);
  if (ageYears < 0 || ageYears > 110) return '';

  return String(month).padStart(2, '0') + '-' + String(day).padStart(2, '0') + '-' + String(year);
}

/**
 * Extract a patient name from records-request phrasing when parsePatientInfoFromText_ fails
 * (records requests are often "Ms. Lastname's report" or "copy of First Last's records" with no DOB).
 * Returns { firstName, lastName } (either may be ''). Pure.
 */
function extractRequestedPatientName_(subject, body) {
  var text = (subject || '') + '\n' + (body || '');
  // "Mr./Ms./Mrs./Dr. Lastname" or "Mr./Ms. Lastname's <doc>" -- no apostrophe in name class
  var m = text.match(/\b(mr|ms|mrs|dr|miss)\.?\s+([A-Za-z][A-Za-z\-]+)(?:'s)?\b/i);
  if (m) return { firstName: '', lastName: capitalizeFirst_(m[2]) };
  // "copy of First Last" / "records for First Last" / "regarding First Last"
  m = text.match(/\b(?:copy of|records? for|regarding|re:|patient)\s+([A-Za-z][A-Za-z\-]+)\s+([A-Za-z][A-Za-z\-]+)(?:'s)?\b/i);
  if (m) return { firstName: capitalizeFirst_(m[1]), lastName: capitalizeFirst_(m[2]) };
  // "First Last's <doc>"
  m = text.match(/\b([A-Za-z][A-Za-z\-]+)\s+([A-Za-z][A-Za-z\-]+)'s\s+(?:report|records?|emg|mri|chart|bill|ledger)/i);
  if (m) return { firstName: capitalizeFirst_(m[1]), lastName: capitalizeFirst_(m[2]) };
  return { firstName: '', lastName: '' };
}

/**
 * Capitalize first letter of each name token, including after hyphen/apostrophe.
 * @param {string} s
 * @returns {string}
 */
function capitalizeFirst_(s) {
  if (!s) return '';
  // Lower-case the whole string first so ALL-CAPS OCR text ("MCCLEARY") and
  // typed text ("McCleary") normalize to the SAME folder name ("Mccleary"),
  // preventing duplicate patient charts.
  return s.toLowerCase().replace(/(^|[\-'])([a-z])/g,
    (_, sep, ch) => sep + ch.toUpperCase());
}

/** Loose RFC-ish check: one @, a dot in the domain, no spaces. Pure. */
function isLikelyEmail_(s) {
  return typeof s === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(s.trim());
}

/** Returns true if s looks like a Sway app report filename or subject ("Sway_...", "Sway-...", "Sway ..."). Pure. */
function isSwayReportName_(s) { return typeof s === 'string' && /^\s*sway[_\-\s]/i.test(s); }
/** Returns true if the subject is self-labeled a referral ("Referral ...", "New Patient Referral: ...", "Re: Referral ..."). Pure. */
function isObviousReferralSubject_(s) { return typeof s === 'string' && /^\s*(re:\s*|fwd:\s*)*(new\s+|patient\s+|incoming\s+)*referral\b/i.test(s); }
/** True if a Gmail attachment content-type is OCR-able by Drive (PDF or image). Pure. */
function isOcrableAttachment_(contentType) {
  var ct = (contentType || '').toLowerCase();
  return ct.indexOf('pdf') !== -1 || ct.indexOf('image/') === 0;
}
/** True if OCR/text of an attached document looks like a patient REFERRAL packet. Pure. */
function looksLikeReferralDoc_(text) {
  if (typeof text !== 'string' || !text) return false;
  return /\bpatient\s+referral\b/i.test(text)
      || /\breferral\s+queue\b/i.test(text)
      || /\bdate\s+of\s+injury\b/i.test(text)
      || /\breferring\s+(physician|provider|doctor)\b/i.test(text);
}

/**
 * Extract patient name and DOB from OCR text of a Sway Clinical Report. Pure.
 * Returns { firstName, lastName, dob, confidence }.
 */
function parseSwayPatient_(ocrText) {
  if (typeof ocrText !== 'string') ocrText = '';
  var firstName = '', lastName = '', dob = '';

  // Name: look for text between "PATIENT NAME" and "DOB"
  var m = ocrText.match(/PATIENT\s*NAME[\s:]*\n?\s*([A-Za-z][A-Za-z'\.\-]*(?:\s+[A-Za-z][A-Za-z'\.\-]*)+?)\s*(?:\n|\s)+DOB/i);
  if (m) {
    var tokens = m[1].trim().split(/\s+/);
    firstName = tokens[0] || '';
    lastName  = tokens[tokens.length - 1] || '';
  }

  // DOB: labeled format M/D/YYYY or MM/DD/YYYY
  var d = ocrText.match(/DOB[\s:]*([0-1]?\d\/[0-3]?\d\/\d{2,4})/i);
  dob = d ? formatDob_(d[1]) : '';

  // Fallback to generic parser if name not found
  if (!firstName || !lastName) {
    var p = parsePatientInfoFromText_('', ocrText);
    if (!firstName)  firstName = p.firstName || '';
    if (!lastName)   lastName  = p.lastName  || '';
    if (!dob)        dob       = p.dob       || '';
  }

  var confidence;
  if (firstName && lastName && dob) {
    confidence = 'high';
  } else if (firstName && lastName) {
    confidence = 'low';
  } else {
    confidence = 'none';
  }

  return { firstName: firstName, lastName: lastName, dob: dob, confidence: confidence };
}

// ---------------------------------------------------------------------------
// # Gate 0 handler - Sway head-injury test reports
// ---------------------------------------------------------------------------

/**
 * Handle a Sway app head-injury/concussion report email.
 * OCRs the attached PDF, extracts patient name/DOB, files to Drive folder.
 * Deterministic - no Gemini call.
 */
function routeSwayReport_(msg, thread, cfg, ledgerSheet, claimRow, bulletin) {
  try {
    var ss        = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
    var ocrCache  = getOrCreateTab_(ss, 'OcrCache', ['Hash', 'ExtractedChars', 'FirstSeen']);
    var attachments = msg.getAttachments({ includeInlineImages: true, includeAttachments: true });

    // Find first OCR-able attachment (PDF or image)
    var pdf = null;
    for (var i = 0; i < attachments.length; i++) {
      if (isOcrableAttachment_(attachments[i].getContentType())) {
        pdf = attachments[i];
        break;
      }
    }

    var ocrText = '';
    if (pdf) {
      try {
        ocrText = extractTextFromPdf_(pdf.copyBlob(), msg.getId(), ocrCache) || '';
      } catch (e) {
        Logger.log('Sway OCR error: ' + e);
      }
    }

    var parsed = parseSwayPatient_(ocrText);

    if (parsed.confidence === 'none') {
      quarantinePdfs_(msg, cfg);
      ledgerUpdate_(ledgerSheet, claimRow, 'review', '', 'none', '', 'Sway test - could not read patient from PDF');
      applyLabel_(thread, cfg.REVIEW_LABEL);
      bulletin.push('[WARN] Sway test - could not read patient from PDF; quarantined for review.');
      return;
    }

    var patientName = (capitalizeFirst_(parsed.firstName) + ' ' + capitalizeFirst_(parsed.lastName)).trim();
    var folder      = getOrCreatePatientFolder_(parsed.firstName, parsed.lastName, parsed.dob, cfg);
    var folderUrl   = folder.getUrl();

    // File every OCR-able attachment (PDF or image) into the patient folder
    for (var j = 0; j < attachments.length; j++) {
      var att = attachments[j];
      if (isOcrableAttachment_(att.getContentType())) {
        try {
          filePdfToFolder_(att.copyBlob(), att.getName(), folder, bulletin);
        } catch (e) {
          bulletin.push('[WARN] Sway: could not file attachment "' + att.getName() + '": ' + e);
        }
      }
    }

    logHeadInjuryPatient_(ss, patientName, parsed.dob || '', 'Sway Test', folderUrl, msg.getId(), bulletin);
    ledgerUpdate_(ledgerSheet, claimRow, 'done', patientName, parsed.confidence || '', folderUrl, 'Sway head-injury test filed to folder');
    logPatientToTracker_(cfg, { name: patientName, folderUrl: folderUrl, where: 'Head Injury', type: 'Head Injury' }, bulletin);
    applyLabel_(thread, cfg.REPORTS_LABEL);
    try { removeLabel_(thread, cfg.REVIEW_LABEL); } catch (_) {}
    try { msg.markRead(); } catch (_) {}
    bulletin.push('[OK] Sway head-injury test filed for ' + patientName + ' -> ' + folderUrl);

  } catch (e) {
    ledgerUpdate_(ledgerSheet, claimRow, 'review', '', '', '', 'Sway handler error: ' + e);
    applyLabel_(thread, cfg.REVIEW_LABEL);
    bulletin.push('[WARN] Sway handler error: ' + e);
  }
}

// ---------------------------------------------------------------------------
// # Gate 3 - Gemini classification
// ---------------------------------------------------------------------------

/**
 * Classify whether the email is a patient referral via Gemini.
 * Returns {isReferral: boolean|null, reason: string}.
 * null means API failure -> caller routes to REVIEW (fail-safe, never fail-open).
 */
/**
 * Call OpenAI Chat Completions. Returns the assistant message string, or null on failure
 * (so callers can fall back to Gemini). Retries 429/500/502/503 with backoff. Never throws.
 * @param {Object} cfg
 * @param {string} systemMsg
 * @param {string} userMsg
 * @param {boolean} wantJson  - request a JSON object response
 * @returns {string|null}
 */
function callOpenAI_(cfg, systemMsg, userMsg, wantJson) {
  var apiKey = cfg.OPENAI_API_KEY;
  if (!apiKey) return null;
  var model = cfg.OPENAI_MODEL || 'gpt-4o-mini';
  var url = 'https://api.openai.com/v1/chat/completions';
  var payload = {
    model: model,
    messages: [
      { role: 'system', content: systemMsg },
      { role: 'user', content: userMsg }
    ],
    temperature: 0
  };
  if (wantJson) payload.response_format = { type: 'json_object' };
  var opts = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    headers: { 'Authorization': 'Bearer ' + apiKey },
    muteHttpExceptions: true
  };
  var retryable = { 429: true, 500: true, 502: true, 503: true };
  var delay = 2000;
  for (var attempt = 0; attempt < 3; attempt++) {
    try {
      var resp = UrlFetchApp.fetch(url, opts);
      var code = resp.getResponseCode();
      if (retryable[code]) {
        Logger.log('OpenAI HTTP ' + code + ', attempt ' + (attempt + 1) + ', retrying in ' + delay + ' ms');
        if (attempt < 2) { Utilities.sleep(delay); delay *= 2; continue; }
        return null;
      }
      if (code !== 200) { Logger.log('OpenAI non-retryable HTTP ' + code + ': ' + resp.getContentText().slice(0, 200)); return null; }
      var json = JSON.parse(resp.getContentText());
      return json.choices && json.choices[0] && json.choices[0].message ? json.choices[0].message.content : null;
    } catch (e) {
      Logger.log('OpenAI exception attempt ' + (attempt + 1) + ': ' + e);
      if (attempt < 2) { Utilities.sleep(delay); delay *= 2; }
    }
  }
  return null;
}

function classifyReferralWithGemini_(subject, text, cfg) {
  // --- OpenAI primary path (falls through to Gemini if key absent or call fails) ---
  if (cfg.OPENAI_API_KEY) {
    var oaiSys = 'You are a clinical intake classifier for a pain-management + surgical practice. '
      + 'Decide whether the email is a patient REFERRAL to this clinic. '
      + 'Respond ONLY with JSON: {"isReferral": true|false, "reason": "<one sentence>"}. '
      + 'TRUE only when a patient is being referred TO this clinic for care. '
      + 'FALSE for vendor invoices/bills the clinic must pay, payment reminders, renewals, '
      + 'account notices, advertisements/marketing/sales pitches, newsletters, and status updates '
      + 'on existing patients. Any email asking OUR practice to pay money is NEVER a referral.';
    var oaiUser = 'SUBJECT: ' + subject + '\n\nBODY (first 3000 chars):\n' + (text || '').slice(0, 3000);
    var oaiRaw = callOpenAI_(cfg, oaiSys, oaiUser, true);
    if (oaiRaw) {
      try {
        var parsed = JSON.parse(oaiRaw);
        return { isReferral: Boolean(parsed.isReferral), reason: parsed.reason || '' };
      } catch (ep) { Logger.log('OpenAI classify parse error: ' + ep); }
    }
    // OpenAI unavailable/unparseable -> fall through to Gemini
  }
  // --- Gemini fallback ---
  const apiKey = cfg.GEMINI_API_KEY;
  if (!apiKey) {
    return {
      isReferral: null,
      reason: 'OpenAI unavailable and GEMINI_API_KEY is not configured.'
    };
  }
  const model  = cfg.GEMINI_MODEL || 'gemini-1.5-flash';
  const url    = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

  const prompt = [
    'You are a clinical intake classifier. Determine whether the following email is a patient REFERRAL.',
    '',
    'Return JSON: { "isReferral": true|false, "reason": "<one sentence>" }',
    '',
    'TRUE only when: a patient is being referred TO this clinic for medical care (e.g. pain management, surgical consult, physical therapy).',
    'FALSE especially for:',
    '- Vendor invoices or bills for software/services/supplies the clinic itself buys',
    '- Payment reminders, receipts, renewals, subscription notices',
    '- Account suspended/overdue/notice emails',
    '- Advertisements, newsletters, marketing',
    '- Status updates on existing patients already being treated',
    '- Any email asking OUR practice to pay money is NEVER a referral, even if it names our doctor.',
    '',
    `SUBJECT: ${subject}`,
    '',
    `BODY (first 3000 chars):\n${text.slice(0, 3000)}`
  ].join('\n');

  const payload = JSON.stringify({
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: {
      responseMimeType: 'application/json',
      responseSchema: {
        type: 'OBJECT',
        properties: {
          isReferral: { type: 'BOOLEAN' },
          reason:     { type: 'STRING' }
        },
        required: ['isReferral', 'reason']
      }
    }
  });

  const opts = {
    method:      'post',
    contentType: 'application/json',
    payload,
    headers:     { 'x-goog-api-key': apiKey },
    muteHttpExceptions: true
  };

  const retryableStatuses = new Set([429, 500, 503]);
  let delay = 2000;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(url, opts);
      const code = resp.getResponseCode();
      if (retryableStatuses.has(code)) {
        Logger.log(`Gemini classify: HTTP ${code}, attempt ${attempt + 1}, retrying in ${delay} ms`);
        Utilities.sleep(delay);
        delay *= 2;
        continue;
      }
      if (code !== 200) {
        Logger.log(`Gemini classify: non-retryable HTTP ${code}`);
        return { isReferral: null, reason: `HTTP ${code}` };
      }
      const json   = JSON.parse(resp.getContentText());
      const result = JSON.parse(json.candidates[0].content.parts[0].text);
      return { isReferral: Boolean(result.isReferral), reason: result.reason || '' };
    } catch (e) {
      Logger.log(`Gemini classify exception attempt ${attempt + 1}: ${e}`);
      if (attempt < 2) { Utilities.sleep(delay); delay *= 2; }
    }
  }
  return { isReferral: null, reason: 'API unreachable after 3 attempts' };
}

/**
 * Multi-class email router (replaces the binary isReferral check at the
 * final classification branch of processThread_). Uses OpenAI when
 * OPENAI_API_KEY is set, else falls back to the Gemini call pattern (mirrors
 * classifyReferralWithGemini_'s retry/backoff). Parses defensively - strips
 * code fences, try/catches JSON.parse - and returns null on ANY failure so
 * the caller can fail safe to review, exactly like the old Gemini failure path.
 * @param {string} subject
 * @param {string} text
 * @param {Object} cfg
 * @returns {?{category: string, patientFirst: string, patientLast: string, reason: string}}
 */
function classifyEmailCategory_(subject, text, cfg) {
  var sysMsg = 'You classify emails for a pain-management clinic inbox. Reply ONLY with JSON: '
    + '{"category":"NEW_REFERRAL|BOOKING_REQUEST|RECORDS_REQUEST|INBOUND_REPORT|PATIENT_OTHER|NOT_PATIENT",'
    + '"patientFirst":"","patientLast":"","reason":""}. '
    + 'NEW_REFERRAL = a provider/attorney refers a NEW patient for treatment. '
    + 'BOOKING_REQUEST = a patient or their representative asks to schedule/reschedule an appointment. '
    + 'RECORDS_REQUEST = someone asks for records, bills, ledgers, or authorization about a patient. '
    + 'INBOUND_REPORT = clinical results/documents about an existing patient (MRI, EMG, labs, op-note). '
    + 'PATIENT_OTHER = mentions a specific patient but fits none of the above. '
    + 'NOT_PATIENT = everything else (marketing, receipts, ops).';
  var userMsg = 'SUBJECT: ' + (subject || '') + '\n\nBODY (first 3000 chars):\n' + String(text || '').slice(0, 3000);

  function parseCategoryJson(raw) {
    if (!raw) return null;
    try {
      var stripped = String(raw).replace(/```json/gi, '').replace(/```/g, '').trim();
      var parsed = JSON.parse(stripped);
      if (!parsed || typeof parsed.category !== 'string') return null;
      return {
        category:     parsed.category,
        patientFirst: parsed.patientFirst || '',
        patientLast:  parsed.patientLast || '',
        reason:       parsed.reason || ''
      };
    } catch (e) {
      Logger.log('classifyEmailCategory_: JSON parse error - ' + e);
      return null;
    }
  }

  // --- OpenAI primary path ---
  if (cfg.OPENAI_API_KEY) {
    var oaiRaw = callOpenAI_(cfg, sysMsg, userMsg, true);
    var oaiResult = parseCategoryJson(oaiRaw);
    if (oaiResult) return oaiResult;
    // OpenAI unavailable/unparseable -> fall through to Gemini
  }

  // --- Gemini fallback (mirrors classifyReferralWithGemini_'s request/retry structure) ---
  const apiKey = cfg.GEMINI_API_KEY;
  if (!apiKey) return null;

  const model = cfg.GEMINI_MODEL || 'gemini-1.5-flash';
  const url   = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

  const payload = JSON.stringify({
    contents: [{ parts: [{ text: sysMsg + '\n\n' + userMsg }] }],
    generationConfig: {
      responseMimeType: 'application/json',
      responseSchema: {
        type: 'OBJECT',
        properties: {
          category:     { type: 'STRING' },
          patientFirst: { type: 'STRING' },
          patientLast:  { type: 'STRING' },
          reason:       { type: 'STRING' }
        },
        required: ['category']
      }
    }
  });

  const opts = {
    method:      'post',
    contentType: 'application/json',
    payload,
    headers:     { 'x-goog-api-key': apiKey },
    muteHttpExceptions: true
  };

  const retryableStatuses = new Set([429, 500, 503]);
  let delay = 2000;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(url, opts);
      const code = resp.getResponseCode();
      if (retryableStatuses.has(code)) {
        Logger.log(`classifyEmailCategory_: Gemini HTTP ${code}, attempt ${attempt + 1}, retrying in ${delay} ms`);
        Utilities.sleep(delay);
        delay *= 2;
        continue;
      }
      if (code !== 200) {
        Logger.log(`classifyEmailCategory_: Gemini non-retryable HTTP ${code}`);
        return null;
      }
      const json = JSON.parse(resp.getContentText());
      return parseCategoryJson(json.candidates[0].content.parts[0].text);
    } catch (e) {
      Logger.log(`classifyEmailCategory_: Gemini exception attempt ${attempt + 1}: ${e}`);
      if (attempt < 2) { Utilities.sleep(delay); delay *= 2; }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// # OCR + filing
// ---------------------------------------------------------------------------

/**
 * Extract text from a PDF blob via Drive's import-as-Google-Doc mechanism.
 * Caches by MessageId+hash to avoid re-OCR on retry within same thread.
 * @param {Blob} blob
 * @param {string} messageId  for cache keying
 * @param {SpreadsheetApp.Sheet} cacheSheet
 * @returns {string}
 */
function extractTextFromPdf_(blob, messageId, cacheSheet) {
  // Compute MD5 hash as a cache key
  const hashBytes = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, blob.getBytes());
  const hashHex   = hashBytes.map(b => ('0' + (b & 0xff).toString(16)).slice(-2)).join('');
  const cacheKey  = messageId + ':' + hashHex;

  // Check cache - just skip duplicates (text is not stored in sheet for PHI reasons)
  const lastRow = cacheSheet.getLastRow();
  if (lastRow > 1) {
    const hashes = cacheSheet.getRange(2, 1, lastRow - 1, 1).getValues().flat().map(String);
    if (hashes.includes(cacheKey)) {
      Logger.log(`OCR cache hit for ${cacheKey} - skipping duplicate attachment.`);
      return '';
    }
  }

  let tempFileId = null;
  const retryStatuses = new Set([403, 429, 500, 502, 503]);
  let delay = 2000;

  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      // Upload PDF as Google Doc via Drive v3 advanced service (triggers OCR).
      const resource = { name: 'tmp_ocr_' + hashHex, mimeType: 'application/vnd.google-apps.document' };

      // Drive advanced service (v3) must be enabled. The media arg MUST be the Blob itself;
      // passing a {mimeType, body} wrapper throws "mediaData parameter only supports Blob types".
      // Preserve image/PDF content-type so Drive OCR handles photos of paper referrals too.
      var ctForOcr = (blob.getContentType() || '').toLowerCase();
      if (ctForOcr.indexOf('image/') !== 0 && ctForOcr.indexOf('pdf') === -1) {
        blob.setContentType('application/pdf');
      }
      const file = Drive.Files.create(resource, blob, { ocrLanguage: 'en' });
      tempFileId = file.id;

      const doc  = DocumentApp.openById(tempFileId);
      const text = doc.getBody().getText();

      // Record in cache
      cacheSheet.appendRow([cacheKey, text.length, new Date().toISOString()]);

      return text;
    } catch (e) {
      const msg = String(e);
      const isRetryable = retryStatuses.has(
        parseInt((msg.match(/(\d{3})/) || [])[1] || '0', 10)
      ) || msg.includes('Quota') || msg.includes('timeout');

      if (isRetryable && attempt < 2) {
        Logger.log(`OCR attempt ${attempt + 1} failed (${e}), retrying in ${delay} ms`);
        Utilities.sleep(delay);
        delay *= 2;
      } else {
        throw e;
      }
    } finally {
      if (tempFileId) {
        try { Drive.Files.remove(tempFileId); } catch (_) {}
        tempFileId = null;
      }
    }
  }
  return '';
}

/**
 * Get or create the patient's Drive folder with the correct naming convention.
 * @param {string} first
 * @param {string} last
 * @param {string} dob  MM-DD-YYYY
 * @param {Object} cfg
 * @returns {DriveApp.Folder}
 */
function getOrCreatePatientFolder_(first, last, dob, cfg) {
  const style    = cfg.FOLDER_NAME_STYLE || 'paren-dob';
  const capFirst = capitalizeFirst_(first);
  const capLast  = capitalizeFirst_(last);

  let folderName;
  if (style === 'underscore') {
    // "First_Last_DOB_YYYY-MM-DD"
    const dobIso = dob ? dob.split('-').join('-') : '';  // already MM-DD-YYYY; reformat to YYYY-MM-DD
    let dobFormatted = '';
    if (dob) {
      const parts = dob.split('-');  // [MM, DD, YYYY]
      dobFormatted = parts.length === 3 ? `${parts[2]}-${parts[0]}-${parts[1]}` : dob;
    }
    folderName = `${capFirst}_${capLast}${dobFormatted ? '_DOB_' + dobFormatted : ''}`;
  } else {
    // 'paren-dob': "First Last (DOB MM-DD-YYYY)"
    folderName = `${capFirst} ${capLast}${dob ? ' (DOB ' + dob + ')' : ''}`;
  }

  const rootFolder = DriveApp.getFolderById(cfg.PATIENTS_ROOT_FOLDER_ID);

  // Search for existing folder by exact name
  const iter = rootFolder.getFoldersByName(folderName);
  if (iter.hasNext()) return iter.next();

  return rootFolder.createFolder(folderName);
}

/**
 * Find an existing patient folder without creating one.
 * Returns a folder only when the match is unambiguous. If DOB is supplied,
 * prefer the matching DOB-bearing folder; otherwise multiple same-name folders
 * return null so staff can review rather than filing to the wrong chart.
 * Returns null if not found or on any Drive error. NEVER creates a folder.
 * @param {string} first
 * @param {string} last
 * @param {Object} cfg
 * @param {string=} dob
 * @returns {DriveApp.Folder|null}
 */
function findExistingPatientFolder_(first, last, cfg, dob) {
  try {
    if (!String(first || '').trim() || !String(last || '').trim()) return null;
    const dobNeedle = normalizeDobForFolderMatch_(dob || '');
    const rootFolder = DriveApp.getFolderById(cfg.PATIENTS_ROOT_FOLDER_ID);
    const iter = rootFolder.getFolders();
    const matches = [];
    while (iter.hasNext()) {
      const folder = iter.next();
      if (matchesPatientFolderName_(folder.getName(), first, last, dob)) matches.push(folder);
    }
    if (matches.length === 0) return null;
    if (dobNeedle) {
      const dobMatches = matches.filter(function(folder) {
        return normalizeDobForFolderMatch_(folder.getName()).indexOf(dobNeedle) !== -1;
      });
      return dobMatches.length === 1 ? dobMatches[0] : null;
    }
    return matches.length === 1 ? matches[0] : null;
  } catch (e) {
    Logger.log(`findExistingPatientFolder_: Drive error - ${e}`);
    return null;
  }
}

/**
 * Normalize DOB-like folder text for matching: "03-04-2006", "03/04/2006",
 * and "DOB 03-04-2006" all become "03042006".
 * @param {string} value
 * @returns {string}
 */
function normalizeDobForFolderMatch_(value) {
  return String(value || '').replace(/\D/g, '');
}

/**
 * True when folderName names the SAME patient as (first, last, dob), tolerating
 * both folder-naming conventions staff actually use:
 *   - "First Last (DOB ...)"   - this system's own convention
 *   - "LAST, FIRST - DOB ..."  - a convention staff sometimes create by hand
 * Case-insensitive. If both the folder name and the supplied dob carry a
 * parseable date, the normalized digit-groups must match; if either side
 * lacks a DOB, the name match alone suffices (folders are not required to
 * carry a DOB in their name).
 * Pure function - no Apps Script globals - unit-testable in Node.
 * @param {string} folderName
 * @param {string} first
 * @param {string} last
 * @param {string} dob
 * @returns {boolean}
 */
function matchesPatientFolderName_(folderName, first, last, dob) {
  var name = String(folderName || '').trim().toLowerCase();
  var f = String(first || '').trim().toLowerCase();
  var l = String(last || '').trim().toLowerCase();
  if (!name || !f || !l) return false;

  var firstLastPrefix = (f + ' ' + l);
  var lastFirstPrefix = (l + ', ' + f);
  var nameMatches = name.indexOf(firstLastPrefix) === 0 || name.indexOf(lastFirstPrefix) === 0;
  if (!nameMatches) return false;

  var folderDobRaw = '';
  var dobMatch = String(folderName || '').match(/(\d{1,4}[\/\-\.]\d{1,2}[\/\-\.]\d{1,4})/);
  if (dobMatch) folderDobRaw = dobMatch[1];

  var folderDobNorm = normalizeDobForFolderMatch_(folderDobRaw);
  var dobNorm = normalizeDobForFolderMatch_(dob || '');

  if (!folderDobNorm || !dobNorm) return true;  // either side lacks a DOB -> name match suffices
  return folderDobNorm === dobNorm;
}

/**
 * Count DISTINCT patient DOBs labeled in a document's text (multi-patient
 * document guard). Scans for every DOB-labeled date ("DOB:", "D.O.B.",
 * "Date of Birth"), normalizes each to digits-only, and counts distinct
 * values. A single patient's DOB repeated in different formats (e.g.
 * "03/04/2006" and "03-04-2006") counts once. Pure function - no Apps
 * Script globals - unit-testable in Node.
 * @param {string} text
 * @returns {number}
 */
function countDistinctDobs_(text) {
  var re = /(?:DOB|D\.O\.B\.?|Date of Birth)[:\s#]*([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})/gi;
  var seen = new Set();
  var m;
  var input = String(text || '');
  while ((m = re.exec(input)) !== null) {
    var norm = normalizeDobForFolderMatch_(m[1]);
    if (norm) seen.add(norm);
  }
  return seen.size;
}

/**
 * Find a patient folder by LAST NAME only (records requests often lack a first name).
 * Returns the first root sub-folder whose name contains the last name (case-insensitive),
 * or null. NEVER creates a folder.
 */
function findPatientFolderByLastName_(last, cfg) {
  try {
    if (!last) return null;
    var needle = String(last).toLowerCase();
    var root = DriveApp.getFolderById(cfg.PATIENTS_ROOT_FOLDER_ID);
    var it = root.getFolders();
    var matches = [];
    while (it.hasNext()) {
      var f = it.next();
      if (f.getName().toLowerCase().indexOf(needle) !== -1) matches.push(f);
    }
    return matches.length === 1 ? matches[0] : null;  // only auto-pick when unambiguous
  } catch (e) {
    Logger.log('findPatientFolderByLastName_: ' + e);
    return null;
  }
}

/**
 * File a PDF blob into a folder; add " (2)", " (3)" on collision and log a warning.
 * @param {Blob} blob
 * @param {string} name
 * @param {DriveApp.Folder} folder
 * @param {string[]} bulletin
 */
function filePdfToFolder_(blob, name, folder, bulletin) {
  const baseName = name.replace(/\.pdf$/i, '');
  let targetName = name;
  let iter = folder.getFilesByName(targetName);
  if (iter.hasNext()) {
    let n = 2;
    while (true) {
      targetName = `${baseName} (${n}).pdf`;
      iter = folder.getFilesByName(targetName);
      if (!iter.hasNext()) break;
      n++;
    }
    bulletin.push(`[WARN] File collision - renamed "${name}" to "${targetName}" (possible reprocessing?)`);
    Logger.log(`filePdfToFolder_: collision renamed "${name}" -> "${targetName}"`);
  }
  blob.setName(targetName);
  folder.createFile(blob);
}

/**
 * Move PDF attachments to QUARANTINE_FOLDER_ID (low-confidence referrals).
 */
function quarantinePdfs_(msg, cfg) {
  if (!cfg.QUARANTINE_FOLDER_ID) return;
  const qFolder = DriveApp.getFolderById(cfg.QUARANTINE_FOLDER_ID);
  for (const att of msg.getAttachments({ includeInlineImages: true, includeAttachments: true })) {
    const ct = (att.getContentType() || '').toLowerCase();
    if (isOcrableAttachment_(ct)) {
      try {
        qFolder.createFile(att.copyBlob().setName(att.getName()));
      } catch (e) {
        Logger.log(`quarantinePdfs_: failed to file "${att.getName()}": ${e}`);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// # Intake form generation
// ---------------------------------------------------------------------------

const MERGE_TAGS = {
  '{{patient_first_name}}': d => d.firstName,
  '{{patient_last_name}}':  d => d.lastName,
  '{{patient_full_name}}':  d => d.fullName,
  '{{dob}}':                d => d.dob,
  '{{date_today}}':         _ => Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM-dd-yyyy'),
  '{{practice_name}}':      _ => 'Atlantic Pain & Wellness Institute',
  '{{practice_phone}}':     _ => ''  // Leave blank; set in a Property if needed
};

/**
 * Copy each template, fill merge tags, export as PDF, trash temp copy.
 * Missing template ID -> skip that form with a log note.
 */
function generateIntakeForms_(data, folderId, cfg, bulletin) {
  const folder     = DriveApp.getFolderById(folderId);
  const templates  = [
    { id: cfg.FEE_SLIP_TEMPLATE_ID,   name: 'Fee Slip' },
    { id: cfg.WC_FORM_TEMPLATE_ID,    name: 'WC Form' },
    { id: cfg.INTAKE_FORM_TEMPLATE_ID, name: 'Intake Form' }
  ];

  for (const tmpl of templates) {
    if (!tmpl.id) {
      Logger.log(`generateIntakeForms_: no template ID for ${tmpl.name} - skipping.`);
      bulletin.push('[WARN] ' + tmpl.name + ' template ID not configured — patient folder is missing this doc.');
      continue;
    }
    let tempFile = null;
    try {
      tempFile = DriveApp.getFileById(tmpl.id).makeCopy(`${tmpl.name} - ${data.fullName} (TEMP)`);
      const doc  = DocumentApp.openById(tempFile.getId());
      const body = doc.getBody();
      for (const [tag, valueFn] of Object.entries(MERGE_TAGS)) {
        body.replaceText(tag.replace(/[{}]/g, '\\$&'), valueFn(data) || '');
      }
      doc.saveAndClose();

      // Export as PDF into the patient folder
      const pdfBlob = tempFile.getAs('application/pdf').setName(`${tmpl.name} - ${data.fullName}.pdf`);
      folder.createFile(pdfBlob);
    } catch (e) {
      Logger.log(`generateIntakeForms_: error on ${tmpl.name}: ${e}`);
      bulletin.push(`[WARN] Could not generate ${tmpl.name}: ${e}`);
    } finally {
      if (tempFile) {
        try { tempFile.setTrashed(true); } catch (_) {}
      }
    }
  }
}

// ---------------------------------------------------------------------------
// # Gemini summary
// ---------------------------------------------------------------------------

/**
 * Generate an administrative summary via Gemini; save as .txt in the folder.
 * On failure: logs and adds review note - never fabricates content.
 */
function generateGeminiSummary_(text, folderId, cfg, patientName, bulletin) {
  // --- OpenAI primary path ---
  if (cfg.OPENAI_API_KEY) {
    var oSys = 'You are a clinical administrative assistant. Write a concise intake summary from the referral text below.\n'
      + 'Use EXACTLY these headings (output plain text, not HTML):\n'
      + 'PATIENT DEMOGRAPHICS & REF\n'
      + 'REFERRING DIAGNOSIS\n'
      + 'INJURIES & SYMPTOMS\n'
      + 'INSURANCE & LEGAL CONTEXT\n'
      + 'ATTORNEY DETAILS\n'
      + '\n'
      + 'Rules:\n'
      + '- For any heading where information is absent, write "not stated" - do NOT invent information.\n'
      + '- Do NOT make diagnostic judgments or treatment recommendations.\n'
      + '- This summary is for administrative use only.';
    var oResult = callOpenAI_(cfg, oSys, 'REFERRAL TEXT:\n' + (text || '').slice(0, 5000), false);
    if (oResult) {
      try {
        var blob = Utilities.newBlob(oResult, 'text/plain', 'Summary - ' + patientName + '.txt');
        DriveApp.getFolderById(folderId).createFile(blob);
        return;
      } catch (es) { Logger.log('OpenAI summary file write error: ' + es); }
    }
    // fall through to Gemini
  }

  // --- Gemini fallback ---
  const apiKey = cfg.GEMINI_API_KEY;
  const model  = cfg.GEMINI_MODEL || 'gemini-1.5-flash';
  const url    = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

  const prompt = [
    'You are a clinical administrative assistant. Write a concise intake summary from the referral text below.',
    'Use EXACTLY these headings (output plain text, not HTML):',
    'PATIENT DEMOGRAPHICS & REF',
    'REFERRING DIAGNOSIS',
    'INJURIES & SYMPTOMS',
    'INSURANCE & LEGAL CONTEXT',
    'ATTORNEY DETAILS',
    '',
    'Rules:',
    '- For any heading where information is absent, write "not stated" - do NOT invent information.',
    '- Do NOT make diagnostic judgments or treatment recommendations.',
    '- This summary is for administrative use only.',
    '',
    `REFERRAL TEXT:\n${text.slice(0, 5000)}`
  ].join('\n');

  const payload = JSON.stringify({
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: { responseMimeType: 'text/plain' }
  });
  const opts = {
    method: 'post', contentType: 'application/json',
    payload, headers: { 'x-goog-api-key': apiKey },
    muteHttpExceptions: true
  };

  const retryable = new Set([429, 500, 503]);
  let delay = 2000;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(url, opts);
      const code = resp.getResponseCode();
      if (retryable.has(code)) {
        if (attempt < 2) { Utilities.sleep(delay); delay *= 2; continue; }
        bulletin.push(`[WARN] Gemini summary failed (HTTP ${code}) - no summary file written.`);
        return;
      }
      if (code !== 200) {
        bulletin.push(`[WARN] Gemini summary failed (HTTP ${code}) - no summary file written.`);
        return;
      }
      const json    = JSON.parse(resp.getContentText());
      const summaryText = json.candidates[0].content.parts[0].text;
      const blob    = Utilities.newBlob(summaryText, 'text/plain', `Summary - ${patientName}.txt`);
      DriveApp.getFolderById(folderId).createFile(blob);
      return;
    } catch (e) {
      Logger.log(`generateGeminiSummary_ attempt ${attempt + 1}: ${e}`);
      if (attempt < 2) { Utilities.sleep(delay); delay *= 2; }
    }
  }
  bulletin.push('[WARN] Gemini summary: API unreachable after 3 attempts - no summary file written.');
}

// ---------------------------------------------------------------------------
// # Head-injury detection
// ---------------------------------------------------------------------------

const HEAD_INJURY_GATE_A = /\b(concussion|tbi|head\s*injury|coup[- ]contrecoup|skull\s*fracture|post.?concuss(ion|ive)?)\b/i;

/**
 * Returns true if the text contains a head-injury indicator AND Gemini confirms it
 * is a CURRENT (not merely historical) head-injury referral.
 * On Gemini API failure: returns false but logs a review note (not confirmed  confirmed).
 */
function detectHeadInjury_(text, cfg) {
  if (!HEAD_INJURY_GATE_A.test(text)) return false;

  // --- Gate B via OpenAI (primary) ---
  if (cfg.OPENAI_API_KEY) {
    var hiSys = 'Answer only TRUE or FALSE. TRUE if this referral is CURRENTLY for a head injury or concussion consult; FALSE if head injury is only past history or incidental.';
    var hiResult = callOpenAI_(cfg, hiSys, (text || '').slice(0, 2000), false);
    if (hiResult !== null) {
      return /^\s*true/i.test(hiResult);
    }
    // OpenAI unavailable -> fall through to Gemini Gate B
  }

  // --- Gate B: Gemini fallback ---
  const apiKey = cfg.GEMINI_API_KEY;
  const model  = cfg.GEMINI_MODEL || 'gemini-1.5-flash';
  const url    = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

  const prompt = [
    'Answer TRUE or FALSE only (no other text).',
    'TRUE if this referral is CURRENTLY for a head injury or concussion consult.',
    'FALSE if head injury/concussion is only mentioned as past history or incidentally.',
    '',
    `TEXT:\n${text.slice(0, 2000)}`
  ].join('\n');

  const opts = {
    method: 'post', contentType: 'application/json',
    payload: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
    headers: { 'x-goog-api-key': apiKey },
    muteHttpExceptions: true
  };

  try {
    const resp = UrlFetchApp.fetch(url, opts);
    if (resp.getResponseCode() !== 200) return false;
    const json   = JSON.parse(resp.getContentText());
    const answer = (json.candidates[0].content.parts[0].text || '').trim().toUpperCase();
    return answer.startsWith('TRUE');
  } catch (e) {
    Logger.log(`detectHeadInjury_ Gate B exception: ${e}`);
    return false;
  }
}

/**
 * Detect case type from text keywords.
 * @param {string} text
 * @returns {'MVA'|'WC'|''}
 */
function detectCaseType_(text) {
  if (/\b(motor\s+vehicle|mva|car\s+accident|auto\s+accident)\b/i.test(text)) return 'MVA';
  if (/\b(work(ers)?[' ]?s?\s+comp(ensation)?|work\s+injury|L&I|on[- ]the[- ]job)\b/i.test(text)) return 'WC';
  return '';
}

/**
 * Append a row to the 'Head Injury' tab with HYPERLINK formulas.
 */
function logHeadInjuryPatient_(ss, patientName, dob, caseType, folderUrl, messageId, bulletin) {
  const sheet = getOrCreateTab_(ss, 'Head Injury', ['Date', 'Patient', 'DOB', 'Case Type', 'Folder', 'MessageId']);
  const folderFormula = folderUrl
    ? `=HYPERLINK("${folderUrl}","Open Folder")`
    : '';
  sheet.appendRow([new Date().toISOString(), patientName, dob, caseType, folderFormula, messageId]);
  bulletin.push(`[HEAD] Head injury routed - ${patientName} (${caseType || 'type unknown'})`);
}

/**
 * Append a new patient to the office master tracker spreadsheet (WC/ASC sheet).
 * Best-effort and non-fatal. Dedupes by Name (col B). Fills only known fields;
 * unknown fields are left blank per the honest-data house rule.
 * @param {Object} cfg
 * @param {Object} f  {name, folderUrl, where, type, phone, referral, attorney}
 * @param {string[]} bulletin
 */
function logPatientToTracker_(cfg, f, bulletin) {
  if (!cfg.PATIENT_TRACKER_SHEET_ID) return;  // feature off until property is set
  try {
    var ss  = SpreadsheetApp.openById(cfg.PATIENT_TRACKER_SHEET_ID);
    var tab = ss.getSheetByName(cfg.PATIENT_TRACKER_TAB);
    if (!tab) {
      Logger.log('logPatientToTracker_: tab "' + cfg.PATIENT_TRACKER_TAB + '" not found - skipping.');
      bulletin.push('[WARN] Tracker tab "' + cfg.PATIENT_TRACKER_TAB + '" not found - patient not added to master sheet.');
      return;
    }
    var name = (f.name || '').trim();
    if (!name) return;

    // Dedupe by Name (column B)
    var lastRow = tab.getLastRow();
    if (lastRow > 1) {
      var names = tab.getRange(2, 2, lastRow - 1, 1).getValues();
      for (var i = 0; i < names.length; i++) {
        if (String(names[i][0]).trim().toLowerCase() === name.toLowerCase()) {
          Logger.log('logPatientToTracker_: "' + name + '" already in tracker - skipping.');
          return;
        }
      }
    }

    var driveCell = f.folderUrl ? '=HYPERLINK("' + f.folderUrl + '","' + name + '")' : name;
    var today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM/dd/yyyy');
    tab.appendRow([
      driveCell,        // Drive Folder
      name,             // Name
      f.where || '',    // Where
      f.type || '',     // Type
      f.phone || '',    // Phone #
      '',               // Last Seen (unknown at intake)
      f.referral || '', // Referral
      f.attorney || '', // Attorney
      today,            // Referral Requested Date
      'Open'            // Status
    ]);
    bulletin.push('[OK] Added ' + name + ' to master tracker (' + cfg.PATIENT_TRACKER_TAB + ').');
  } catch (e) {
    Logger.log('logPatientToTracker_ error: ' + e);
    bulletin.push('[WARN] Could not add ' + (f.name || '(name)') + ' to master tracker: ' + e);
  }
}

// ---------------------------------------------------------------------------
// # Reporting
// ---------------------------------------------------------------------------

/**
 * Prepend a dated run block to the LOG_DOC_ID Google Doc.
 * Errors/quarantines are rendered in red; successes in green.
 */
function writeToDocBulletin_(lines, cfg) {
  if (!cfg.LOG_DOC_ID || !lines.length) return;
  const doc  = DocumentApp.openById(cfg.LOG_DOC_ID);
  const body = doc.getBody();

  // Insert at position 0 (prepend)
  const header = body.insertParagraph(0, `Run: ${new Date().toISOString()}`);
  header.setHeading(DocumentApp.ParagraphHeading.HEADING3);

  let insertAfter = 1;
  for (const line of lines) {
    const isError   = /^[ERR]/.test(line);
    const isWarning = /^[WARN]/.test(line);
    const isSuccess = /^[OK]/.test(line);
    const para = body.insertParagraph(insertAfter, line);
    if (isError || isWarning) {
      para.setForegroundColor('#CC0000');
    } else if (isSuccess) {
      para.setForegroundColor('#006600');
    }
    insertAfter++;
  }
  // Separator
  body.insertParagraph(insertAfter, '-'.repeat(60));
}

/**
 * Write a color-coded per-run summary row to the "Run Summary" tab (newest on top).
 * Tallies this run's ledger rows (Timestamp >= startMs) by outcome. Best-effort, never throws.
 */
function writeRunSummary_(cfg, startMs, threadsEvaluated, isSweep) {
  try {
    var ss = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
    var ledger = ss.getSheetByName('Runs');
    var referrals = 0, records = 0, reports = 0, ignored = 0, review = 0, errors = 0;
    var patients = [], attention = [];
    if (ledger && ledger.getLastRow() > 1) {
      var rows = ledger.getRange(2, 1, ledger.getLastRow() - 1, LEDGER_HEADERS.length).getValues();
      for (var i = 0; i < rows.length; i++) {
        var ts = new Date(rows[i][0]).getTime();
        if (!(ts >= startMs)) continue;
        var status = String(rows[i][2]);
        var who = String(rows[i][3] || '').trim();
        if (status === 'done') { referrals++; if (who) patients.push(who); }
        else if (status === 'records') { records++; if (who) patients.push(who + ' (records)'); }
        else if (status === 'report-filed') { reports++; if (who) patients.push(who + ' (report)'); }
        else if (status === 'ignored') { ignored++; }
        else if (status === 'review') { review++; if (who) attention.push(who + ' - review'); else attention.push('(unnamed) - review'); }
        else if (status === 'error' || status === 'stale-claim') { errors++; attention.push((who || '(unnamed)') + ' - ' + status); }
      }
    }
    var tab = getOrCreateTab_(ss, 'Run Summary', ['Run Time','Type','Duration (s)','Threads','Referrals Filed','Records','Reports','Ignored','Needs Review','Errors','Patients Filed','Needs Attention']);
    // Style the header once
    var headerRange = tab.getRange(1, 1, 1, 12);
    headerRange.setFontWeight('bold').setFontColor('#ffffff').setBackground('#004d40');
    tab.setFrozenRows(1);
    // Insert newest run at row 2 (top, under header)
    tab.insertRowBefore(2);
    var durationS = Math.round((Date.now() - startMs) / 1000);
    var rowVals = [[
      Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM-dd-yyyy HH:mm'),
      isSweep ? 'Backlog Sweep' : 'Live',
      durationS, threadsEvaluated || 0,
      referrals, records, reports, ignored, review, errors,
      patients.join(', '), attention.join(', ')
    ]];
    var rowRange = tab.getRange(2, 1, 1, 12);
    rowRange.setValues(rowVals);
    // Color the row: green when clean, light red when anything needs attention
    var clean = (review === 0 && errors === 0);
    rowRange.setBackground(clean ? '#E8F5E9' : '#FFEBEE');
    if (!clean) tab.getRange(2, 9, 1, 2).setFontColor('#B71C1C').setFontWeight('bold'); // Needs Review + Errors cells
    try { tab.autoResizeColumns(1, 12); } catch (_) {}
  } catch (e) {
    Logger.log('writeRunSummary_ error: ' + e);
  }
}

/**
 * Separate trigger-able function: email the script owner a digest of
 * review/stale-claim rows from the last 24 hours.
 */
function sendQuarantineDigest() {
  const cfg = loadConfig_();
  if (!cfg.LOG_SHEET_ID) { Logger.log('sendQuarantineDigest: LOG_SHEET_ID not set.'); return; }

  const ss    = SpreadsheetApp.openById(cfg.LOG_SHEET_ID);
  const sheet = ss.getSheetByName('Runs');
  if (!sheet) { Logger.log('sendQuarantineDigest: Runs sheet not found.'); return; }

  const cutoff  = Date.now() - 24 * 60 * 60 * 1000;
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;

  const data = sheet.getRange(2, 1, lastRow - 1, LEDGER_HEADERS.length).getValues();
  const rows = data.filter(row => {
    const ts     = new Date(row[0]).getTime();
    const status = String(row[2]);
    return ts >= cutoff && (status === 'review' || status === 'stale-claim' || status === 'error' || status === 'records');
  });

  if (!rows.length) {
    Logger.log('sendQuarantineDigest: no review/error rows in last 24 h.');
    return;
  }

  const lines = rows.map(r => `[${r[2]}] ${r[3] || '(no name)'} - ${r[6] || ''}`);
  const body  = `Atlantic Pain & Wellness - Intake Digest\n` +
                `${rows.length} item(s) need review in the last 24 hours:\n\n` +
                lines.join('\n') +
                `\n\nCheck the Runs tab in the log spreadsheet for details.`;

  const digestTo = cfg.NOTIFY_EMAIL || Session.getEffectiveUser().getEmail();
  GmailApp.sendEmail(digestTo, `[Intake] ${rows.length} item(s) need review`, body);
  Logger.log(`sendQuarantineDigest: emailed digest of ${rows.length} row(s).`);
}

// ---------------------------------------------------------------------------
// # Utility helpers
// ---------------------------------------------------------------------------

/** Get or create a sheet tab with given headers. */
function getOrCreateTab_(ss, name, headers) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
    sheet.appendRow(headers);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

/** Apply a Gmail label to a thread, creating the label if it doesn't exist. */
function applyLabel_(thread, labelName) {
  let label = GmailApp.getUserLabelByName(labelName);
  if (!label) label = GmailApp.createLabel(labelName);
  thread.addLabel(label);
}

/** Remove a Gmail label from a thread (silently if not present). */
function removeLabel_(thread, labelName) {
  const label = GmailApp.getUserLabelByName(labelName);
  if (label) thread.removeLabel(label);
}

// ---------------------------------------------------------------------------
// # In-script unit tests
// ---------------------------------------------------------------------------

function assert_(condition, message) {
  if (!condition) {
    Logger.log(`FAIL: ${message}`);
    throw new Error(`FAIL: ${message}`);
  }
  Logger.log(`PASS: ${message}`);
}

/**
 * runUnitTests - run from the Apps Script editor to verify pure functions.
 * All tests should log PASS; any FAIL throws.
 */
function runUnitTests() {
  Logger.log('=== runUnitTests START ===');

  // formatDob_ - century pivot
  assert_(formatDob_('01/15/85') === '01-15-1985',   'formatDob_ 2-digit >=currentYear+1 -> 1900s');
  assert_(formatDob_('01/15/10') === '01-15-2010',   'formatDob_ 2-digit <currentYear+1 -> 2000s');
  assert_(formatDob_('1990-06-15') === '06-15-1990', 'formatDob_ ISO YYYY-MM-DD');
  assert_(formatDob_('Jun 15, 1990') === '06-15-1990', 'formatDob_ written month');
  assert_(formatDob_('13/01/1990') === '',            'formatDob_ invalid month 13 -> empty');
  assert_(formatDob_('02/30/1990') === '',            'formatDob_ Feb 30 -> empty');
  assert_(formatDob_('') === '',                      'formatDob_ empty string -> empty');
  // Age outside range
  assert_(formatDob_('01/01/1900') === '',            'formatDob_ age >110 -> empty');
  // Future date -> age < 0
  assert_(formatDob_('01/01/2099') === '',            'formatDob_ future date -> empty');

  // capitalizeFirst_ - hyphen and apostrophe
  assert_(capitalizeFirst_('smith-jones') === 'Smith-Jones', 'capitalizeFirst_ hyphen');
  assert_(capitalizeFirst_("o'brien")     === "O'Brien",     "capitalizeFirst_ apostrophe");
  assert_(capitalizeFirst_('mcallen')     === 'Mcallen',     'capitalizeFirst_ mc (no special case)');
  assert_(capitalizeFirst_('')            === '',            'capitalizeFirst_ empty');

  // parsePatientInfoFromText_ - salutation exclusion
  {
    const subj = 'Re: Dr. Smith referral';
    const body = 'Dear Dr. Smith,\nPlease see patient Jane Doe DOB: 03-15-1975\n';
    const r = parsePatientInfoFromText_(subj, body);
    assert_(r.firstName !== 'Smith', 'salutation exclusion: "Dr. Smith" must not be patient');
  }

  // parsePatientInfoFromText_ - labeled DOB only
  {
    const body = 'RE: John Miller\nAccident date: 2025-01-01\nPatient: Miller, John DOB: 04-20-1980\n';
    const r = parsePatientInfoFromText_('', body);
    assert_(r.dob === '04-20-1980',          'labeled DOB extracted correctly');
    assert_(r.dobSource === 'labeled',        'dobSource is labeled');
    assert_(r.firstName === 'John',           'firstName from labeled pattern');
  }

  // parsePatientInfoFromText_ - bare date is NOT DOB
  {
    const body = 'Patient was seen on 01/10/2026. Name: Jane Smith.\n';
    const r = parsePatientInfoFromText_('', body);
    assert_(r.dob === '',                    'bare date without DOB label -> no dob');
  }

  // isImplausibleName_ - name-plausibility guard
  assert_(isImplausibleName_('Please', 'Hello')             === true,  'isImplausibleName_ "Please Hello" -> true');
  assert_(isImplausibleName_('Zztest', 'Referralcheck')     === false, 'isImplausibleName_ non-blocklist nonsense -> false');
  assert_(isImplausibleName_('John', 'Smith')               === false, 'isImplausibleName_ ordinary name -> false');
  assert_(isImplausibleName_('', 'Smith')                   === true,  'isImplausibleName_ empty first -> true');
  assert_(isImplausibleName_('J', 'Smith')                  === true,  'isImplausibleName_ single-letter first -> true');
  assert_(isImplausibleName_('Records', 'Request')          === true,  'isImplausibleName_ "Records Request" -> true');

  // isVendorInvoice_ - true positive
  {
    const subject = 'Overdue Invoice From Sway Medical';
    const body    = 'Your account payment of $499 is past due. Please log in to pay.';
    assert_(isVendorInvoice_(subject, body),   'vendor invoice positive: overdue invoice');
  }

  // isVendorInvoice_ - false on referral mentioning billing
  {
    const subject = 'Patient Referral - Jane Doe';
    const body    = 'Please see Jane Doe, DOB 01/05/1990. Billing: Workers Comp. Date of birth confirmed.';
    assert_(!isVendorInvoice_(subject, body),  'vendor invoice negative: referral with billing mention');
  }

  // Staff blocklist  (simulate by calling parsePatientInfoFromText_ after injecting property)
  // Note: in node test harness this is exercised directly via a cfg-aware wrapper.
  // Here we just verify the blocklist string split logic.
  {
    const rawBl  = 'John Doe, Jane Smith';
    const parsed = rawBl.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    assert_(parsed.includes('john doe'),   'blocklist split includes "john doe"');
    assert_(parsed.includes('jane smith'), 'blocklist split includes "jane smith"');
  }

  // detectCaseType_
  assert_(detectCaseType_('The patient was injured in an MVA on the highway') === 'MVA', 'detectCaseType_ MVA');
  assert_(detectCaseType_('Workers Comp claim filed after on-the-job injury')  === 'WC',  'detectCaseType_ WC');
  assert_(detectCaseType_('Routine physical exam')                             === '',   'detectCaseType_ empty');

  // Suffix handling
  {
    const body = 'Patient: Williams, Robert Jr.\nDOB: 07-04-1975';
    const r    = parsePatientInfoFromText_('', body);
    assert_(r.lastName.startsWith('Williams'), 'suffix: last name is Williams (suffix stripped from pattern)');
  }

  // MVA + WC priority (MVA wins if both present - current impl: MVA checked first)
  assert_(detectCaseType_('MVA and also workers comp') === 'MVA', 'detectCaseType_ MVA takes precedence');

  // isTerminalLedgerStatus_ - terminal statuses
  assert_(isTerminalLedgerStatus_('records')      === true, 'isTerminalLedgerStatus_ records -> terminal');
  assert_(isTerminalLedgerStatus_('review')       === true, 'isTerminalLedgerStatus_ review -> terminal');
  assert_(isTerminalLedgerStatus_('error')        === true, 'isTerminalLedgerStatus_ error -> terminal');
  assert_(isTerminalLedgerStatus_('report-filed') === true, 'isTerminalLedgerStatus_ report-filed -> terminal');
  assert_(isTerminalLedgerStatus_('stale-claim')  === true, 'isTerminalLedgerStatus_ stale-claim -> terminal');
  assert_(isTerminalLedgerStatus_('done')         === true, 'isTerminalLedgerStatus_ done -> terminal');
  assert_(isTerminalLedgerStatus_('ignored')      === true, 'isTerminalLedgerStatus_ ignored -> terminal');
  // isTerminalLedgerStatus_ - non-terminal statuses
  assert_(isTerminalLedgerStatus_('claimed')      === false, 'isTerminalLedgerStatus_ claimed -> not terminal');
  assert_(isTerminalLedgerStatus_('retry')        === false, 'isTerminalLedgerStatus_ retry -> not terminal');
  assert_(isTerminalLedgerStatus_('')             === false, 'isTerminalLedgerStatus_ empty -> not terminal');
  assert_(isTerminalLedgerStatus_(undefined)      === false, 'isTerminalLedgerStatus_ undefined -> not terminal');

  // isIntakeAlertSubject_
  assert_(isIntakeAlertSubject_('[Intake Alert] New referral: X — call to book') === true,
    'isIntakeAlertSubject_ plain alert subject -> true');
  assert_(isIntakeAlertSubject_('Re: [Intake Alert] New referral: X — call to book') === true,
    'isIntakeAlertSubject_ Re: prefix -> true');
  assert_(isIntakeAlertSubject_('[Intake] 3 item(s) need review') === true,
    'isIntakeAlertSubject_ digest subject -> true');
  assert_(isIntakeAlertSubject_('New Patient Referral') === false,
    'isIntakeAlertSubject_ ordinary referral subject -> false');

  // buildReferralAlertBody_
  {
    const r = buildReferralAlertBody_('Jose Cartagena', '04-02-1968', 'Workers Comp', false,
      '555-123-4567', 'Dr. Smith', 'https://drive.google.com/drive/folders/abc123');
    assert_(r.subject.indexOf('[Intake Alert] ') === 0, 'buildReferralAlertBody_ subject starts with [Intake Alert] ');
    assert_(r.plain.indexOf('https://drive.google.com/drive/folders/abc123') !== -1,
      'buildReferralAlertBody_ plain body contains folder URL');
    assert_(r.plain.indexOf('undefined') === -1, 'buildReferralAlertBody_ plain body never contains "undefined"');
  }

  // matchesPatientFolderName_
  assert_(matchesPatientFolderName_('Asad Grant (DOB 01-11-1999)', 'Asad', 'Grant', '01-11-1999') === true,
    'matchesPatientFolderName_ First Last (DOB dash) matches same DOB dash');
  assert_(matchesPatientFolderName_('GRANT, ASAD - DOB 01/11/1999', 'Asad', 'Grant', '01-11-1999') === true,
    'matchesPatientFolderName_ LAST, FIRST - DOB slash matches First Last dash dob (cross-format)');
  assert_(matchesPatientFolderName_('grant, asad - dob 01/11/1999', 'ASAD', 'GRANT', '01/11/1999') === true,
    'matchesPatientFolderName_ case-insensitive both sides');
  assert_(matchesPatientFolderName_('Asad Grant', 'Asad', 'Grant', '') === true,
    'matchesPatientFolderName_ name match suffices when neither side has a DOB');
  assert_(matchesPatientFolderName_('Asad Grant (DOB 01-11-1999)', 'Asad', 'Grant', '') === true,
    'matchesPatientFolderName_ folder has DOB but supplied dob is empty -> name match suffices');
  assert_(matchesPatientFolderName_('Asad Grant (DOB 05-06-2000)', 'Asad', 'Grant', '01-11-1999') === false,
    'matchesPatientFolderName_ mismatched DOB -> false');
  assert_(matchesPatientFolderName_('Someone Else (DOB 01-11-1999)', 'Asad', 'Grant', '01-11-1999') === false,
    'matchesPatientFolderName_ mismatched name -> false');
  assert_(matchesPatientFolderName_('Grantham, Asadollah - DOB 01-11-1999', 'Asad', 'Grant', '01-11-1999') === false,
    'matchesPatientFolderName_ prefix must be exact token, not substring of a longer surname');

  // isIgnoredSender_
  assert_(isIgnoredSender_('Invoice+Statements@Mail.Anthropic.com', 'invoice+statements@mail.anthropic.com,alerts@tdbank.com') === true,
    'isIgnoredSender_ case-insensitive exact match -> true');
  assert_(isIgnoredSender_('someone@notondlist.com', 'invoice+statements@mail.anthropic.com,alerts@tdbank.com') === false,
    'isIgnoredSender_ sender not on list -> false');
  assert_(isIgnoredSender_('billing@example.com', '') === false,
    'isIgnoredSender_ empty list -> false');

  // isPartnerBillingSender_
  assert_(isPartnerBillingSender_('Jane Doe <jane@mdmanage.com>', 'mdmanage.com,srm-inc.com') === true,
    'isPartnerBillingSender_ exact domain match -> true');
  assert_(isPartnerBillingSender_('jane@billing.mdmanage.com', 'mdmanage.com,srm-inc.com') === true,
    'isPartnerBillingSender_ subdomain match -> true');
  assert_(isPartnerBillingSender_('jane@notmdmanage.com', 'mdmanage.com') === false,
    'isPartnerBillingSender_ lookalike domain (no dot boundary) -> false');
  assert_(isPartnerBillingSender_('jane@othercompany.com', 'mdmanage.com,srm-inc.com') === false,
    'isPartnerBillingSender_ unrelated domain -> false');

  // parseFaxNotification_
  {
    const r = parseFaxNotification_('New Fax Message from (469) 543-6401 on 07/01/2026 6:03 PM', 'You have a new fax.\nPages: 5\n');
    assert_(r !== null, 'parseFaxNotification_ fax subject parses -> non-null');
    assert_(r.fromNumber === '(469) 543-6401', 'parseFaxNotification_ extracts fromNumber');
    assert_(r.pages === 5, 'parseFaxNotification_ extracts pages');
  }
  assert_(parseFaxNotification_('New Fax Message from (469) 543-6401 on 07/01/2026', '') !== null &&
    parseFaxNotification_('New Fax Message from (469) 543-6401 on 07/01/2026', '').pages === 0,
    'parseFaxNotification_ missing Pages line -> pages 0');
  assert_(parseFaxNotification_('New Voice Message from (469) 543-6401 on 07/01/2026 6:03 PM', '') === null,
    'parseFaxNotification_ voicemail subject rejected by fax regex -> null');
  assert_(parseFaxNotification_('Re: Your invoice is ready', '') === null,
    'parseFaxNotification_ non-RingCentral subject -> null');

  // countDistinctDobs_
  assert_(countDistinctDobs_('No dates of birth here at all.') === 0,
    'countDistinctDobs_ no DOBs -> 0');
  assert_(countDistinctDobs_('Patient DOB: 03/04/2006, seen for follow-up.') === 1,
    'countDistinctDobs_ single DOB -> 1');
  assert_(countDistinctDobs_('Patient 1 DOB: 03/04/2006\nPatient 2 DOB: 11-22-1985\n') === 2,
    'countDistinctDobs_ two distinct DOBs -> 2');
  assert_(countDistinctDobs_('DOB: 03/04/2006 ... later in doc D.O.B. 03-04-2006') === 1,
    'countDistinctDobs_ same DOB repeated in slash and dash formats -> counts once');

  // isBookingRequest_
  assert_(isBookingRequest_('Harun Omar Sahin - REQ FOR APPOINTMENT', '') === true,
    'isBookingRequest_ "REQ FOR APPOINTMENT" subject -> true');
  assert_(isBookingRequest_('Scheduling', 'can we schedule the patient') === true,
    'isBookingRequest_ "can we schedule the patient" -> true');
  assert_(isBookingRequest_('REQ for Medical Records & Billing', 'please send records') === false,
    'isBookingRequest_ records/billing keeps priority -> false');
  assert_(isBookingRequest_('Invoice', 'Your invoice is past due') === false,
    'isBookingRequest_ invoice -> false');

  // isOwnAddress_
  assert_(isOwnAddress_('mainlinepain@gmail.com', 'mainlinesurgery@gmail.com,mainlinepain@gmail.com,mainsurgical@gmail.com') === true,
    'isOwnAddress_ own address -> true');
  assert_(isOwnAddress_('Dr <mainlinepain@gmail.com>', 'mainlinesurgery@gmail.com,mainlinepain@gmail.com,mainsurgical@gmail.com') === true,
    'isOwnAddress_ display-name form -> true');
  assert_(isOwnAddress_('someone@foreignfirm.com', 'mainlinesurgery@gmail.com,mainlinepain@gmail.com,mainsurgical@gmail.com') === false,
    'isOwnAddress_ foreign address -> false');

  // buildBookingAlertBody_
  {
    const r = buildBookingAlertBody_('Harun Omar Sahin', 'attorney@example.com', '', '', 'REQ FOR APPOINTMENT');
    assert_(r.subject.indexOf('[Intake Alert] ') === 0, 'buildBookingAlertBody_ subject starts with [Intake Alert] ');
    assert_(r.plain.indexOf('undefined') === -1, 'buildBookingAlertBody_ plain body never contains "undefined"');
    assert_(r.plain.indexOf('—') !== -1, 'buildBookingAlertBody_ missing phone/folder render em-dash');
  }

  Logger.log('=== runUnitTests COMPLETE - all PASS ===');
}

// ---------------------------------------------------------------------------
// # Draft email generation (CHANGES C & D)
// ---------------------------------------------------------------------------

/**
 * Call Gemini to extract patient/attorney contact fields from referral text.
 * Returns a plain object; never throws (returns {} on any failure).
 *
 * @param {string} fullText  - full OCR+body text of the referral
 * @param {Object} cfg       - config from loadConfig_
 * @returns {Object}         - {patientEmail, patientPhone, attorneyName, attorneyEmail,
 *                              accidentDate, missingDocs}
 */
function extractReferralContacts_(fullText, cfg) {
  if (!cfg.DRAFT_PATIENT_EMAILS) return {};

  // --- OpenAI primary path ---
  if (cfg.OPENAI_API_KEY) {
    var cSys = 'Extract referral contact fields from the medical referral text. Respond ONLY with JSON with '
      + 'these exact keys: {"patientPhone":"","patientEmail":"","insuranceCarrier":"","insuranceId":"",'
      + '"claimNumber":"","referral":"","attorneyName":"","attorneyFirm":"","attorneyPhone":"","attorneyEmail":"",'
      + '"accidentDate":"","missingDocs":[]}. Extract ONLY values literally present; use "" (or [] for missingDocs) '
      + 'when absent. Never invent. accidentDate as YYYY-MM-DD or "".';
    var cRaw = callOpenAI_(cfg, cSys, 'REFERRAL TEXT (first 4000 chars):\n' + (fullText || '').slice(0, 4000), true);
    if (cRaw) {
      try {
        var r = JSON.parse(cRaw);
        return {
          patientPhone: r.patientPhone || '', patientEmail: r.patientEmail || '',
          insuranceCarrier: r.insuranceCarrier || '', insuranceId: r.insuranceId || '',
          claimNumber: r.claimNumber || '', referral: r.referral || '',
          attorneyName: r.attorneyName || '', attorneyFirm: r.attorneyFirm || '',
          attorneyPhone: r.attorneyPhone || '', attorneyEmail: r.attorneyEmail || '',
          accidentDate: r.accidentDate || '', missingDocs: Array.isArray(r.missingDocs) ? r.missingDocs : []
        };
      } catch (ec) { Logger.log('OpenAI contacts parse error: ' + ec); }
    }
    // fall through to Gemini
  }

  // --- Gemini fallback ---
  const apiKey = cfg.GEMINI_API_KEY;
  if (!apiKey) {
    Logger.log('extractReferralContacts_: no GEMINI_API_KEY - skipping');
    return {};
  }

  const model = cfg.GEMINI_MODEL || 'gemini-1.5-flash';
  const url   = `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`;

  const prompt = [
    'Extract contact details from the medical referral text below.',
    'Return STRICT JSON with exactly these keys:',
    '{"patientEmail":"","patientPhone":"","attorneyName":"","attorneyEmail":"","accidentDate":"","missingDocs":[]}',
    '',
    'Rules:',
    '- Extract ONLY values literally present in the text. Never invent or guess.',
    '- Use "" for any string field not found; use [] for missingDocs when not found.',
    '- accidentDate: use YYYY-MM-DD format, or "" if not found.',
    '- missingDocs: list only documents explicitly stated as missing or needed from the patient.',
    '',
    `REFERRAL TEXT (first 4000 chars):\n${fullText.slice(0, 4000)}`
  ].join('\n');

  const payload = JSON.stringify({
    contents: [{ parts: [{ text: prompt }] }],
    generationConfig: { responseMimeType: 'application/json' }
  });

  const opts = {
    method:      'post',
    contentType: 'application/json',
    payload,
    headers:     { 'x-goog-api-key': apiKey },
    muteHttpExceptions: true
  };

  const retryableStatuses = new Set([429, 500, 503]);
  let delay = 2000;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = UrlFetchApp.fetch(url, opts);
      const code = resp.getResponseCode();
      if (retryableStatuses.has(code)) {
        Logger.log(`extractReferralContacts_: HTTP ${code}, attempt ${attempt + 1}, retrying in ${delay} ms`);
        Utilities.sleep(delay);
        delay *= 2;
        continue;
      }
      if (code !== 200) {
        Logger.log(`extractReferralContacts_: non-retryable HTTP ${code}`);
        return {};
      }
      const json    = JSON.parse(resp.getContentText());
      const rawText = json.candidates[0].content.parts[0].text;
      const result  = JSON.parse(rawText);
      // Defensive: ensure expected shape
      return {
        patientEmail:  result.patientEmail  || '',
        patientPhone:  result.patientPhone  || '',
        attorneyName:  result.attorneyName  || '',
        attorneyEmail: result.attorneyEmail || '',
        accidentDate:  result.accidentDate  || '',
        missingDocs:   Array.isArray(result.missingDocs) ? result.missingDocs : []
      };
    } catch (e) {
      Logger.log(`extractReferralContacts_ exception attempt ${attempt + 1}: ${e}`);
      if (attempt < 2) { Utilities.sleep(delay); delay *= 2; }
    }
  }
  Logger.log('extractReferralContacts_: API unreachable after 3 attempts - returning {}');
  return {};
}

/**
 * Create Gmail DRAFT emails for patient and/or attorney. Never auto-sends.
 * Appends status notes to bulletin. Never throws.
 *
 * @param {Object} parsed    - from parsePatientInfoFromText_
 * @param {string} nameFull  - "First Last"
 * @param {Object} contacts  - from extractReferralContacts_
 * @param {Object} cfg       - config from loadConfig_
 * @param {Array}  bulletin  - mutable bulletin array
 */
function createReferralDrafts_(parsed, nameFull, contacts, cfg, bulletin) {
  if (!cfg.DRAFT_PATIENT_EMAILS) return;
  try {
    // Guard: EmailTemplates.gs must be pasted in to provide builder functions.
    if (typeof buildPatientWelcomeEmail !== 'function' ||
        typeof buildPatientMissingDocsEmail !== 'function' ||
        typeof buildAttorneyInquiryEmail !== 'function') {
      bulletin.push('Email templates file not found - paste EmailTemplates.gs to enable drafts.');
      return;
    }

    // -- Patient draft --------------------------------------------------------
    const hasMissingDocs = Array.isArray(contacts.missingDocs) && contacts.missingDocs.length > 0;
    let patientSubject, patientHtml, patientPlain;

    if (hasMissingDocs) {
      patientSubject = 'Action Required: Finish Your Pre-Registration File - Patient Intake Department';
      patientHtml    = buildPatientMissingDocsEmail(nameFull, contacts.missingDocs);
      patientPlain   = 'Hello ' + nameFull + ',\n\nOur records show that we are still missing some documents needed to complete your pre-registration. Please contact our office so we can assist you.';
    } else {
      patientSubject = 'Welcome! Action Required: Complete Your Pre-Visit Forms';
      patientHtml    = buildPatientWelcomeEmail(nameFull, cfg.PATIENT_FORM_URL);
      patientPlain   = 'Hello ' + nameFull + ',\n\nWelcome to our practice! Please complete your pre-visit intake forms at: ' + (cfg.PATIENT_FORM_URL || '[form link not configured]');
    }

    if (isLikelyEmail_(contacts.patientEmail)) {
      GmailApp.createDraft(contacts.patientEmail, patientSubject, patientPlain, { htmlBody: patientHtml });
      bulletin.push('[MAIL] Patient welcome DRAFT created for ' + contacts.patientEmail + ' - review before sending.');
    } else {
      bulletin.push('[MAIL] No patient email found - welcome draft skipped for ' + nameFull + '.');
    }

    // -- Attorney draft -------------------------------------------------------
    if (isLikelyEmail_(contacts.attorneyEmail)) {
      const attSubject = 'Legal-Medical Coordination Case Inquiry - Client: ' + nameFull;
      const attHtml    = buildAttorneyInquiryEmail(contacts.attorneyName || '', nameFull, contacts.accidentDate || '');
      const attPlain   = 'Dear Counselor,\n\nOur clinic has received a medical referral for your client, ' + nameFull + '. Please verify the litigation status of this case and if active medical liens or LOP apply.';
      GmailApp.createDraft(contacts.attorneyEmail, attSubject, attPlain, { htmlBody: attHtml });
      bulletin.push('[MAIL] Attorney coordination DRAFT created for ' + contacts.attorneyEmail + ' - review before sending.');
    } else if (contacts.attorneyName) {
      bulletin.push('[MAIL] Attorney "' + contacts.attorneyName + '" mentioned but no email found - attorney draft skipped.');
    }
    // No bulletin note when neither attorney name nor email is present (most referrals).

  } catch (e) {
    Logger.log('createReferralDrafts_ error: ' + e);
    bulletin.push('[WARN] Draft email creation failed (filing succeeded): ' + e);
  }
}

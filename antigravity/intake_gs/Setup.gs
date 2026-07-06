/**
 * intake_gs/Setup.gs - Atlantic Pain & Wellness Institute
 * One-time cutover helper for the Code.gs intake refactor.
 *
 * Run order:
 *   1) bootstrapFromLegacySettings  - migrate legacy Settings tab -> Script Properties;
 *                                     create Quarantine folder + Run-Log doc (idempotent).
 *   2) runUnitTests                 - lives in Code.gs; verify parsing is correct.
 *   3) dryRun                       - lives in Code.gs; confirm email scan without side-effects.
 *   4) installIntakeTriggers        - remove ALL existing triggers (disables the old Code.js
 *                                     trigger) and install processInbox (30 min) +
 *                                     sendQuarantineDigest (daily 7 am).
 *
 * Use cutoverStatus() at any time to print current config + trigger state.
 */

'use strict';

// ---------------------------------------------------------------------------
// # Legacy -> new property name mapping (reference)
//
//   Legacy key (Settings tab A col)    ->  New Script Property name
//   GEMINI_API_KEY                     ->  GEMINI_API_KEY
//   ROOT_DRIVE_FOLDER_ID               ->  PATIENTS_ROOT_FOLDER_ID
//   PATIENT_INTAKE_TEMPLATE_ID         ->  INTAKE_FORM_TEMPLATE_ID
//   FEE_TEMPLATE_ID                    ->  FEE_SLIP_TEMPLATE_ID
//   WORKERS_COMP_TEMPLATE_ID           ->  WC_FORM_TEMPLATE_ID
//   STAFF_NAME_BLOCKLIST               ->  STAFF_NAME_BLOCKLIST
// ---------------------------------------------------------------------------

// Retired/404 Gemini model names that must NOT be carried forward.
const RETIRED_GEMINI_MODELS_ = ['gemini-2.0-flash', 'gemini-2.0-flash-lite'];

// Preferred model to set when none is already configured (or current is retired).
const DEFAULT_GEMINI_MODEL_ = 'gemini-2.5-flash-lite';

// ---------------------------------------------------------------------------
// # Private helpers
// ---------------------------------------------------------------------------

/**
 * Read the "Settings" tab from `ss` into a plain {key: trimmedStringValue} object.
 * Uses the same A=key / B=value / data-from-row-2 logic as Config.js getSystemConfig().
 * Returns an empty object if the tab is missing or empty.
 *
 * @param {SpreadsheetApp.Spreadsheet} ss
 * @returns {Object.<string,string>}
 */
function readLegacySettings_(ss) {
  const legacy = {};
  const settingsSheet = ss.getSheetByName('Settings');
  if (!settingsSheet) {
    Logger.log('WARNING: "Settings" tab not found in the spreadsheet. ' +
               'Proceeding with infrastructure creation only (no property migration).');
    return legacy;
  }

  const lastRow = settingsSheet.getLastRow();
  if (lastRow < 2) {
    Logger.log('WARNING: "Settings" tab is empty (no data below header row). ' +
               'Proceeding with infrastructure creation only.');
    return legacy;
  }

  // Columns A (index 0) and B (index 1), rows 2..lastRow
  const values = settingsSheet.getRange(2, 1, lastRow - 1, 2).getValues();
  for (let i = 0; i < values.length; i++) {
    const key = String(values[i][0]).trim();
    if (!key) continue;
    const val = (typeof values[i][1] === 'string')
      ? values[i][1].trim()
      : String(values[i][1]);
    legacy[key] = val;
  }
  return legacy;
}

/**
 * Test whether a Drive folder id is currently accessible.
 * @param {string} id
 * @returns {boolean}
 */
function drivefolderExists_(id) {
  if (!id) return false;
  try {
    DriveApp.getFolderById(id); // throws if id is invalid or no access
    return true;
  } catch (_) {
    return false;
  }
}

/**
 * Test whether a Document id is currently accessible.
 * @param {string} id
 * @returns {boolean}
 */
function docExists_(id) {
  if (!id) return false;
  try {
    DocumentApp.openById(id); // throws if id is invalid or no access
    return true;
  } catch (_) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// # Public functions
// ---------------------------------------------------------------------------

/**
 * STEP 1 - Migrate legacy Settings tab -> Script Properties.
 * Creates the Quarantine folder and Run-Log doc as needed.
 * Safe to re-run: existing valid ids are never overwritten.
 */
function bootstrapFromLegacySettings() {
  const props = PropertiesService.getScriptProperties();
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  if (!ss) {
    Logger.log('ERROR: No active spreadsheet. Open this script from its bound ' +
               'log spreadsheet, then run again.');
    return;
  }

  // -- 1. Read legacy Settings tab ------------------------------------------
  const legacy = readLegacySettings_(ss);

  // -- 2. Map legacy keys -> new Script Property names -----------------------
  // Only set when the legacy value is non-empty.

  // GEMINI_API_KEY - migrate but never log its value.
  if (legacy.GEMINI_API_KEY) {
    props.setProperty('GEMINI_API_KEY', legacy.GEMINI_API_KEY);
    // Value is intentionally not logged.
  }

  // PATIENTS_ROOT_FOLDER_ID <- ROOT_DRIVE_FOLDER_ID
  if (legacy.ROOT_DRIVE_FOLDER_ID) {
    props.setProperty('PATIENTS_ROOT_FOLDER_ID', legacy.ROOT_DRIVE_FOLDER_ID);
  }

  // INTAKE_FORM_TEMPLATE_ID <- PATIENT_INTAKE_TEMPLATE_ID
  if (legacy.PATIENT_INTAKE_TEMPLATE_ID) {
    props.setProperty('INTAKE_FORM_TEMPLATE_ID', legacy.PATIENT_INTAKE_TEMPLATE_ID);
  }

  // FEE_SLIP_TEMPLATE_ID <- FEE_TEMPLATE_ID
  if (legacy.FEE_TEMPLATE_ID) {
    props.setProperty('FEE_SLIP_TEMPLATE_ID', legacy.FEE_TEMPLATE_ID);
  }

  // WC_FORM_TEMPLATE_ID <- WORKERS_COMP_TEMPLATE_ID
  if (legacy.WORKERS_COMP_TEMPLATE_ID) {
    props.setProperty('WC_FORM_TEMPLATE_ID', legacy.WORKERS_COMP_TEMPLATE_ID);
  }

  // STAFF_NAME_BLOCKLIST - carry over if present
  if (legacy.STAFF_NAME_BLOCKLIST) {
    props.setProperty('STAFF_NAME_BLOCKLIST', legacy.STAFF_NAME_BLOCKLIST);
  }

  // PATIENT_FORM_URL <- PRE_VISIT_FORM_URL (only if non-empty and not a placeholder viewform URL)
  if (legacy.PRE_VISIT_FORM_URL &&
      legacy.PRE_VISIT_FORM_URL.indexOf('/d/e/') === -1) {
    props.setProperty('PATIENT_FORM_URL', legacy.PRE_VISIT_FORM_URL);
  }

  // DRAFT_PATIENT_EMAILS - set default 'true' only if not already present
  if (!props.getProperty('DRAFT_PATIENT_EMAILS')) {
    props.setProperty('DRAFT_PATIENT_EMAILS', 'true');
  }

  // DRAFT_RECORDS_REPLIES - set default 'true' only if not already present (CHANGE D)
  if (!props.getProperty('DRAFT_RECORDS_REPLIES')) {
    props.setProperty('DRAFT_RECORDS_REPLIES', 'true');
  }

  // NOTIFY_ON_REFERRAL - instant new-referral alert; set default '1' (ON) only if not already present
  if (!props.getProperty('NOTIFY_ON_REFERRAL')) {
    props.setProperty('NOTIFY_ON_REFERRAL', '1');
  }
  // NOTIFY_EMAIL - optional; left unset means "send to self" (effective user). Never defaulted here.

  // -- 3. Always set LOG_SHEET_ID to the bound spreadsheet id ---------------
  props.setProperty('LOG_SHEET_ID', ss.getId());
  Logger.log('LOG_SHEET_ID set to: ' + ss.getId());

  // -- 4. Resolve GEMINI_MODEL -----------------------------------------------
  // Keep existing value only if it is set AND not a retired model.
  // Never copy the legacy value if it is retired.
  const existingModel = props.getProperty('GEMINI_MODEL') || '';
  const legacyModel   = legacy.GEMINI_MODEL || '';

  let chosenModel = DEFAULT_GEMINI_MODEL_;

  if (existingModel && !RETIRED_GEMINI_MODELS_.includes(existingModel)) {
    // Already have a good model in Script Properties - keep it.
    chosenModel = existingModel;
  } else if (legacyModel && !RETIRED_GEMINI_MODELS_.includes(legacyModel)) {
    // Legacy has a non-retired model we haven't seen before - adopt it.
    chosenModel = legacyModel;
  }
  // Otherwise fall back to DEFAULT_GEMINI_MODEL_.

  props.setProperty('GEMINI_MODEL', chosenModel);
  Logger.log('GEMINI_MODEL set to: ' + chosenModel);

  // -- 5. Labels + INBOX_QUERY - set only if not already present ------------
  const labelDefaults = {
    PROCESSED_LABEL: 'patient-pdfs-processed',
    REVIEW_LABEL:    'patient-pdfs-needs-review',
    IGNORED_LABEL:   'patient-pdfs-ignored',
    RECORDS_LABEL:   'patient-records-billing',
    REPORTS_LABEL:   'patient-reports-inbound',
    INBOX_QUERY:     'in:inbox'
  };
  for (const [key, defaultVal] of Object.entries(labelDefaults)) {
    if (!props.getProperty(key)) {
      props.setProperty(key, defaultVal);
      Logger.log(key + ' set to default: ' + defaultVal);
    }
  }

  // -- 6. Quarantine folder (idempotent) -------------------------------------
  let quarantineFolderId = props.getProperty('QUARANTINE_FOLDER_ID') || '';
  if (quarantineFolderId && drivefolderExists_(quarantineFolderId)) {
    Logger.log('Quarantine folder already exists - keeping id: ' + quarantineFolderId);
  } else {
    // Create it fresh.
    const rootId = props.getProperty('PATIENTS_ROOT_FOLDER_ID') || '';
    let quarantineFolder;
    if (rootId && drivefolderExists_(rootId)) {
      const rootFolder = DriveApp.getFolderById(rootId);
      quarantineFolder = rootFolder.createFolder('APW Intake - Quarantine');
      Logger.log('Created Quarantine folder inside root folder.');
    } else {
      quarantineFolder = DriveApp.createFolder('APW Intake - Quarantine');
      Logger.log('PATIENTS_ROOT_FOLDER_ID not set/invalid - created Quarantine folder at Drive root.');
    }
    quarantineFolderId = quarantineFolder.getId();
    props.setProperty('QUARANTINE_FOLDER_ID', quarantineFolderId);
    Logger.log('QUARANTINE_FOLDER_ID set to: ' + quarantineFolderId);
  }

  // -- 7. Run-Log doc (idempotent) -------------------------------------------
  let logDocId = props.getProperty('LOG_DOC_ID') || '';
  if (logDocId && docExists_(logDocId)) {
    Logger.log('Run-Log doc already exists - keeping id: ' + logDocId);
  } else {
    const logDoc = DocumentApp.create('APW Intake - Run Log');
    logDocId = logDoc.getId();
    props.setProperty('LOG_DOC_ID', logDocId);
    Logger.log('LOG_DOC_ID set to: ' + logDocId);
  }

  // -- 8. Final report -------------------------------------------------------
  Logger.log('');
  Logger.log('===== bootstrapFromLegacySettings - Result =====');

  const required = {
    LOG_SHEET_ID:           { hint: 'Set automatically from bound spreadsheet - re-run if missing.' },
    PATIENTS_ROOT_FOLDER_ID:{ hint: 'Paste the Drive folder id into the Settings tab ROOT_DRIVE_FOLDER_ID row, then re-run.' },
    QUARANTINE_FOLDER_ID:   { hint: 'Created automatically - if missing, check Drive permissions and re-run.' },
    LOG_DOC_ID:             { hint: 'Created automatically - if missing, check Drive permissions and re-run.' },
    GEMINI_API_KEY:         { hint: 'Paste it into the Settings tab GEMINI_API_KEY row, then re-run.', secret: true }
  };

  for (const [key, meta] of Object.entries(required)) {
    const val = props.getProperty(key) || '';
    if (val) {
      if (meta.secret) {
        Logger.log('  ' + key + ': OK - set (hidden)');
      } else {
        Logger.log('  ' + key + ': OK - ' + val);
      }
    } else {
      Logger.log('  ' + key + ': MISSING - ' + meta.hint);
    }
  }

  Logger.log('  GEMINI_MODEL:          ' + (props.getProperty('GEMINI_MODEL') || DEFAULT_GEMINI_MODEL_));
  Logger.log('  OPENAI_API_KEY:        ' + (props.getProperty('OPENAI_API_KEY') ? 'OK - set (hidden)' : 'MISSING-optional (add Script Property OPENAI_API_KEY to use OpenAI first)'));
  Logger.log('  OPENAI_MODEL:          ' + (props.getProperty('OPENAI_MODEL') || 'gpt-4o-mini'));
  Logger.log('  Quarantine folder id:  ' + quarantineFolderId);
  Logger.log('  Run-Log doc id:        ' + logDocId);

  // Draft email properties report
  const patientFormUrl       = props.getProperty('PATIENT_FORM_URL') || '';
  const draftPatientEmails   = props.getProperty('DRAFT_PATIENT_EMAILS') || 'true';
  const draftRecordsReplies  = props.getProperty('DRAFT_RECORDS_REPLIES') || 'true';
  Logger.log('  PATIENT_FORM_URL:       ' + (patientFormUrl ? 'OK - ' + patientFormUrl : 'MISSING-optional (set PRE_VISIT_FORM_URL in Settings tab to populate)'));
  Logger.log('  DRAFT_PATIENT_EMAILS:   ' + draftPatientEmails);
  Logger.log('  DRAFT_RECORDS_REPLIES:  ' + draftRecordsReplies);

  // Instant new-referral alert properties report
  const notifyOnReferral = props.getProperty('NOTIFY_ON_REFERRAL') || '1';
  const notifyEmail      = props.getProperty('NOTIFY_EMAIL') || '';
  Logger.log('  NOTIFY_ON_REFERRAL:     ' + notifyOnReferral + (notifyOnReferral !== '0' ? ' (alert ON)' : ' (alert OFF)'));
  Logger.log('  NOTIFY_EMAIL:           ' + (notifyEmail ? notifyEmail : '(not set - alerts go to effective user / self)'));
  Logger.log('================================================');
}

/**
 * STEP 4 - Remove ALL existing project triggers and install the two
 * production triggers for the new Code.gs intake script.
 *
 * NOTE: processInbox and sendQuarantineDigest must exist in Code.gs.
 *       Deleting all triggers here also removes the legacy Code.js trigger.
 */
function installIntakeTriggers() {
  // Remove every existing trigger.
  const existing = ScriptApp.getProjectTriggers();
  const removedCount = existing.length;
  for (const trigger of existing) {
    ScriptApp.deleteTrigger(trigger);
  }
  Logger.log('Removed ' + removedCount + ' existing trigger(s) ' +
             '(this also disables any legacy Code.js trigger).');

  // Install processInbox - every 30 minutes.
  // processInbox must be defined in Code.gs.
  ScriptApp.newTrigger('processInbox')
    .timeBased()
    .everyMinutes(30)
    .create();
  Logger.log('Installed trigger: processInbox (time-based, every 30 minutes).');

  // Install sendQuarantineDigest - daily at 7 am.
  // sendQuarantineDigest must be defined in Code.gs.
  ScriptApp.newTrigger('sendQuarantineDigest')
    .timeBased()
    .everyDays(1)
    .atHour(7)
    .create();
  Logger.log('Installed trigger: sendQuarantineDigest (time-based, daily at 7 am).');

  Logger.log('installIntakeTriggers complete. Active triggers: 2.');
}

/**
 * Read-only status check - print current Script Properties and trigger state.
 * Safe to run at any time during or after cutover.
 */
function cutoverStatus() {
  const props = PropertiesService.getScriptProperties();

  Logger.log('===== cutoverStatus =====');
  Logger.log('-- Script Properties --');

  const allProps = props.getProperties();
  const displayOrder = [
    'LOG_SHEET_ID',
    'PATIENTS_ROOT_FOLDER_ID',
    'QUARANTINE_FOLDER_ID',
    'LOG_DOC_ID',
    'GEMINI_API_KEY',
    'GEMINI_MODEL',
    'OPENAI_API_KEY',
    'OPENAI_MODEL',
    'INBOX_QUERY',
    'PROCESSED_LABEL',
    'REVIEW_LABEL',
    'IGNORED_LABEL',
    'RECORDS_LABEL',
    'REPORTS_LABEL',
    'FEE_SLIP_TEMPLATE_ID',
    'WC_FORM_TEMPLATE_ID',
    'INTAKE_FORM_TEMPLATE_ID',
    'MAX_THREADS',
    'TIME_BUDGET_MS',
    'FOLDER_NAME_STYLE',
    'STAFF_NAME_BLOCKLIST',
    'SKIP_VENDOR_INVOICES',
    'NOTIFY_ON_REFERRAL',
    'NOTIFY_EMAIL'
  ];

  for (const key of displayOrder) {
    const val = allProps[key] !== undefined ? allProps[key] : '';
    if (key === 'GEMINI_API_KEY' || key === 'OPENAI_API_KEY') {
      Logger.log('  ' + key + ': ' + (val ? 'set (hidden)' : 'MISSING'));
    } else {
      Logger.log('  ' + key + ': ' + (val || '(not set)'));
    }
  }

  // Any extra properties not in the display list
  for (const key of Object.keys(allProps)) {
    if (!displayOrder.includes(key)) {
      Logger.log('  ' + key + ': ' + allProps[key] + '  [extra]');
    }
  }

  Logger.log('');
  Logger.log('-- Active Triggers --');
  const triggers = ScriptApp.getProjectTriggers();
  if (triggers.length === 0) {
    Logger.log('  (none)');
  } else {
    for (const t of triggers) {
      Logger.log('  ' + t.getHandlerFunction() + ' (' + t.getEventType() + ')');
    }
  }

  Logger.log('');
  Logger.log('-- Required Properties Check --');
  const required5 = ['LOG_SHEET_ID', 'PATIENTS_ROOT_FOLDER_ID', 'QUARANTINE_FOLDER_ID', 'LOG_DOC_ID', 'GEMINI_API_KEY'];
  let allPass = true;
  for (const key of required5) {
    const val = allProps[key] || '';
    const pass = !!val;
    if (!pass) allPass = false;
    if (key === 'GEMINI_API_KEY') {
      Logger.log('  ' + (pass ? 'PASS' : 'MISSING') + '  ' + key + (pass ? ' (set, hidden)' : ''));
    } else {
      Logger.log('  ' + (pass ? 'PASS' : 'MISSING') + '  ' + key);
    }
  }
  Logger.log(allPass ? 'Overall: READY FOR PRODUCTION' : 'Overall: NOT READY - fix MISSING items above.');
  Logger.log('=========================');
}

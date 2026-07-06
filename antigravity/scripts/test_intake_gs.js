/**
 * scripts/test_intake_gs.js
 * Node.js test harness for intake_gs/Code.gs pure functions.
 *
 * Usage:  ~/.local/node-runtime/bin/node scripts/test_intake_gs.js
 * Exits non-zero on any FAIL.
 */

'use strict';

const fs   = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Stub Apps Script globals so Code.gs can be eval'd in Node
// ---------------------------------------------------------------------------

// PropertiesService — return empty blocklist by default; tests override via
// global.__BLOCKLIST__ before calling parsePatientInfoFromText_.
global.PropertiesService = {
  getScriptProperties: () => ({
    getProperties:  () => ({}),
    getProperty:    (k) => (k === 'STAFF_NAME_BLOCKLIST' ? (global.__BLOCKLIST__ || '') : null)
  })
};

// Logger → console
global.Logger = { log: (msg) => { /* suppress during test; use console for failures */ } };

// Stubs for APIs that pure functions don't call but top-level declarations reference
global.SpreadsheetApp  = {};
global.GmailApp        = {};
global.DriveApp        = {};
global.DocumentApp     = {};
global.MailApp         = {};
global.UrlFetchApp     = {};
global.Utilities = {
  sleep:          ()  => {},
  formatDate:     ()  => '01-01-2026',
  computeDigest:  ()  => [],
  DigestAlgorithm: { MD5: 'MD5' },
  newBlob:        ()  => ({})
};
global.Session     = { getScriptTimeZone: () => 'UTC', getEffectiveUser: () => ({ getEmail: () => '' }) };
global.Drive       = { Files: { create: () => ({ id: 'tmp' }), remove: () => {} } };

// ---------------------------------------------------------------------------
// Load Code.gs
// ---------------------------------------------------------------------------

const gsPath = path.resolve(__dirname, '../intake_gs/Code.gs');
const gsCode = fs.readFileSync(gsPath, 'utf8');

// Remove 'use strict' from the top of the file before eval so the
// function declarations land on global scope cleanly in non-strict eval.
const evalCode = gsCode.replace(/^['"]use strict['"];?\s*/m, '');

// Wrap in a function that receives our stub globals and returns all declared names.
// Using new Function() so function declarations land in a shared scope we can read back.
let gsModule;
try {
  // Build a wrapper: inject every global stub, then execute the GAS code and return
  // all top-level function names so we can copy them onto global.
  const wrapper = new Function(
    // named parameters — each stub global
    'PropertiesService', 'Logger', 'SpreadsheetApp', 'GmailApp', 'DriveApp',
    'DocumentApp', 'MailApp', 'UrlFetchApp', 'Utilities', 'Session', 'Drive',
    // body
    evalCode + '\n' +
    // Return an object mapping every function name to its value
    'return {' +
    [
      'formatDob_', 'capitalizeFirst_', 'parsePatientInfoFromText_', 'isImplausibleName_',
      'isVendorInvoice_', 'isBulkMail_', 'detectCaseType_', 'detectHeadInjury_',
      'isRecordsOrBillingRequest_', 'isInboundPatientReport_',
      'loadConfig_', 'processInbox', 'dryRun', 'runUnitTests',
      'ledgerClaim_', 'ledgerUpdate_', 'openLedger_', 'classifyReferralWithGemini_',
      'extractTextFromPdf_', 'getOrCreatePatientFolder_', 'filePdfToFolder_',
      'generateIntakeForms_', 'generateGeminiSummary_', 'logHeadInjuryPatient_',
      'writeToDocBulletin_', 'sendQuarantineDigest', 'getOrCreateTab_',
      'applyLabel_', 'removeLabel_', 'quarantinePdfs_', 'hasSalutation_',
      'assert_', 'isLikelyEmail_', 'buildRecordsReplyBody_',
      'isSwayReportName_', 'parseSwayPatient_', 'isObviousReferralSubject_',
      'isOcrableAttachment_', 'looksLikeReferralDoc_',
      'isSalesSolicitation_', 'extractRequestedPatientName_',
      'normalizeDobForFolderMatch_',
      'isTerminalLedgerStatus_', 'isIntakeAlertSubject_', 'buildReferralAlertBody_',
      'retryReviewQueue',
      'matchesPatientFolderName_', 'findExistingPatientFolder_', 'findPatientFolderByLastName_',
      'isIgnoredSender_', 'isPartnerBillingSender_', 'routeRecordsBillingRequest_',
      'parseFaxNotification_', 'routeInboundReport_',
      'countDistinctDobs_',
      'isOwnAddress_', 'isBookingRequest_', 'buildBookingAlertBody_',
      'routeBookingRequest_', 'routeThreadFollowup_', 'classifyEmailCategory_'
    ].map(n => `"${n}": typeof ${n} !== "undefined" ? ${n} : undefined`).join(',') +
    '};'
  );

  gsModule = wrapper(
    global.PropertiesService, global.Logger, global.SpreadsheetApp,
    global.GmailApp, global.DriveApp, global.DocumentApp, global.MailApp,
    global.UrlFetchApp, global.Utilities, global.Session, global.Drive
  );

  // Expose all exported functions as globals so tests can call them directly
  for (const [name, fn] of Object.entries(gsModule)) {
    if (typeof fn === 'function') global[name] = fn;
  }
} catch (e) {
  console.error('FATAL: failed to load Code.gs into test harness:', e.message);
  console.error(e.stack);
  process.exit(1);
}

// Verify critical functions are available
const required = ['formatDob_', 'capitalizeFirst_', 'parsePatientInfoFromText_',
                   'isVendorInvoice_', 'detectCaseType_'];
for (const fn of required) {
  if (typeof global[fn] !== 'function') {
    console.error(`FATAL: ${fn} not exported from Code.gs`);
    process.exit(1);
  }
}

// ---------------------------------------------------------------------------
// Test framework
// ---------------------------------------------------------------------------

let passed = 0, failed = 0;

function assert(condition, message) {
  if (condition) {
    console.log(`  PASS: ${message}`);
    passed++;
  } else {
    console.error(`  FAIL: ${message}`);
    failed++;
  }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

console.log('\n=== formatDob_ ===');

// Century pivot: two-digit year >= (currentYear%100 + 1) → 1900s
// Current year is 2026 → threshold is 27
// 85 >= 27 → 1985
assert(formatDob_('01/15/85') === '01-15-1985',    '2-digit 85 → 1985');
// 10 < 27 → 2010
assert(formatDob_('01/15/10') === '01-15-2010',    '2-digit 10 → 2010');
assert(formatDob_('1990-06-15') === '06-15-1990',  'ISO YYYY-MM-DD');
assert(formatDob_('Jun 15, 1990') === '06-15-1990','written month');
assert(formatDob_('13/01/1990') === '',             'month 13 → empty');
assert(formatDob_('02/30/1990') === '',             'Feb 30 → empty');
assert(formatDob_('') === '',                       'empty → empty');
// Age > 110
const veryOld = '01/01/' + (new Date().getFullYear() - 115);
assert(formatDob_(veryOld) === '',                  'age >110 → empty');
// Future date → negative age
assert(formatDob_('01/01/2099') === '',             'future date → empty');
// Valid recent DOB
assert(formatDob_('03/15/1975') === '03-15-1975',  'valid 1975 DOB');

console.log('\n=== capitalizeFirst_ ===');

assert(capitalizeFirst_('smith-jones') === 'Smith-Jones', 'hyphen');
assert(capitalizeFirst_("o'brien")     === "O'Brien",     'apostrophe');
assert(capitalizeFirst_('mcallen')     === 'Mcallen',     'mc prefix (no special rule)');
assert(capitalizeFirst_('JANE')        === 'Jane',         'all-caps normalized to title case');
assert(capitalizeFirst_('')            === '',             'empty string');
assert(capitalizeFirst_('anne-marie')  === 'Anne-Marie',  'double hyphen name');
assert(capitalizeFirst_('MCCLEARY')    === 'Mccleary',  'capitalizeFirst_ all-caps OCR -> title case');
assert(capitalizeFirst_('NICHOL')      === 'Nichol',    'capitalizeFirst_ all-caps single name');

console.log('\n=== salutation exclusion ===');

{
  // "Dear Dr. Smith" on same line before the name → must NOT become patient
  const body = 'Dear Dr. Smith,\nPlease see patient Jane Doe DOB: 03-15-1975\n';
  const r = parsePatientInfoFromText_('Re: Dr. Smith referral', body);
  assert(r.firstName !== 'Smith', 'salutation "Dr. Smith" excluded');
}
{
  // "Hi Dr. Jones" exclusion
  const body = 'Hi Dr. Jones,\nReferring patient: Bob Brown DOB: 06/20/1968\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.firstName !== 'Jones', 'salutation "Dr. Jones" excluded');
}

console.log('\n=== labeled-DOB-only rule ===');

{
  // Labeled DOB must be extracted
  const body = 'Patient: Miller, John\nDOB: 04-20-1980\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.dob === '04-20-1980',     'labeled DOB extracted');
  assert(r.dobSource === 'labeled',  'dobSource is labeled');
}
{
  // Bare date (accident date) must NOT become DOB
  const body = 'Patient was seen on 01/10/2026. Name: Jane Smith.\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.dob === '', 'bare date without label → no dob');
}
{
  // Two dates: only labeled one should be DOB
  const body = 'Accident: 06/01/2024\nPatient: Adams, Alice DOB: 11/22/1985\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.dob === '11-22-1985', 'correct labeled DOB selected among multiple dates');
}

console.log('\n=== isImplausibleName_ (name-plausibility guard) ===');

assert(isImplausibleName_('Please', 'Hello')         === true,  '"Please Hello" -> true');
assert(isImplausibleName_('Zztest', 'Referralcheck') === false, 'non-blocklist nonsense -> false');
assert(isImplausibleName_('John', 'Smith')           === false, 'ordinary name -> false');
assert(isImplausibleName_('', 'Smith')               === true,  'empty first -> true');
assert(isImplausibleName_('J', 'Smith')              === true,  'single-letter first -> true');
assert(isImplausibleName_('Records', 'Request')      === true,  '"Records Request" -> true');

console.log('\n=== vendor invoice filter ===');

{
  const subj = 'Overdue Invoice From Sway Medical';
  const body = 'Your account payment of $499 is past due. Please log in to pay your subscription.';
  assert(isVendorInvoice_(subj, body),   'vendor invoice positive: overdue invoice');
}
{
  const subj = 'Patient Referral - Jane Doe';
  const body = 'Please see Jane Doe, DOB 01/05/1990. Billing: Workers Comp. Date of birth confirmed.';
  assert(!isVendorInvoice_(subj, body),  'vendor invoice negative: referral with billing mention');
}
{
  // Invoice subject but body mentions referral — should NOT be flagged
  const subj = 'Invoice for referral services';
  const body = 'Referral for patient John Smith, date of birth 05/12/1982, injured in MVA.';
  assert(!isVendorInvoice_(subj, body),  'vendor invoice negative: invoice subject but referral body');
}
{
  // Pure marketing — has Precedence header equivalent check (isBulkMail_ test via raw)
  // We can't fully test isBulkMail_ without a mock message, but we can test isVendorInvoice_
  const subj = 'Renewal Notice — your subscription expires soon';
  const body = 'Please renew your account before the expiry date.';
  assert(isVendorInvoice_(subj, body),   'renewal notice with no referral evidence → vendor invoice');
}

console.log('\n=== staff blocklist ===');

{
  global.__BLOCKLIST__ = 'John Doe, Dr. Jane Smith';
  const body = 'Patient: Doe, John\nDOB: 05/05/1980\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.confidence === 'blocked', 'blocklist match → confidence blocked');
  global.__BLOCKLIST__ = '';
}
{
  global.__BLOCKLIST__ = 'John Doe';
  const body = 'Patient: Smith, Alice\nDOB: 07/07/1990\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.confidence !== 'blocked', 'non-blocked name → not blocked');
  global.__BLOCKLIST__ = '';
}

console.log('\n=== MVA / WC detection ===');

assert(detectCaseType_('Patient injured in MVA on I-95')                     === 'MVA', 'MVA keyword');
assert(detectCaseType_('motor vehicle accident on 06/01/2024')               === 'MVA', 'motor vehicle keyword');
assert(detectCaseType_('Workers Comp claim, injured at work')                === 'WC',  'Workers Comp keyword');
assert(detectCaseType_('Work injury, L&I claim number 123')                  === 'WC',  'L&I keyword');
assert(detectCaseType_('Routine follow-up for low back pain')                === '',   'no case type → empty string');
assert(detectCaseType_('MVA and also on-the-job workers comp claim')         === 'MVA', 'MVA takes precedence');

console.log('\n=== suffix handling ===');

{
  const body = 'Patient: Williams, Robert Jr.\nDOB: 07-04-1975\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.lastName === 'Williams', 'Jr suffix: last name is Williams');
  assert(r.firstName === 'Robert',  'Jr suffix: first name is Robert');
  assert(r.dob === '07-04-1975',    'Jr suffix: DOB correct');
}
{
  const body = 'Patient: Torres, Maria III\nDOB: 01/01/1965\n';
  const r = parsePatientInfoFromText_('', body);
  assert(r.lastName === 'Torres', 'Roman numeral suffix: last name Torres');
}

console.log('\n=== name with hyphen/accented letters ===');

{
  const body = 'Patient: López-García, María\nDOB: 09/12/1978\n';
  const r = parsePatientInfoFromText_('', body);
  // The regex should handle accented letters
  assert(r.dob === '09-12-1978', 'accented name: DOB extracted');
}
{
  const body = "Patient Name: O'Connor Patrick\nDOB: 03/22/1987\n";
  const r = parsePatientInfoFromText_('', body);
  assert(r.dob === '03-22-1987', "apostrophe in name: DOB extracted");
}

console.log('\n=== records & billing request ===');

// POSITIVES — must return true
{
  const subj = 'Records Request - Darlene Turner';
  const body = 'Please send us a copy of the medical records for Darlene Turner, treated at your facility in 2024.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: attorney copy-of-records');
}
{
  const subj = 'WC claim #12345 — insurance verification';
  const body = 'Updated claim number for patient. WC claim #12345, insurance verification of benefits needed.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: insurance/claim update with claim number');
}
{
  const subj = 'Itemized Billing Statement — Balance Due';
  const body = 'Requesting an itemized billing ledger / balance due for services rendered to our client.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: itemized bill ledger balance due');
}
{
  const subj = 'Letter of Protection — Maria Hernandez';
  const body = 'Enclosed is a letter of protection (LOP) for the above-named patient. Please continue treatment.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: LOP / letter of protection');
}
{
  const subj = 'Subpoena — patient records';
  const body = 'You are hereby served a subpoena for all medical records pertaining to James O\'Brien.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: subpoena keyword');
}
{
  const subj = 'EOB — non-par adjustment';
  const body = 'Please review the attached explanation of benefits. Non-par adjustment applied. Outstanding balance remains.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: EOB / explanation of benefits / non-par');
}
{
  const subj = 'Pre-authorization required';
  const body = 'Insurance pre-authorization is needed before the procedure can be scheduled.';
  assert(isRecordsOrBillingRequest_(subj, body), 'records request: pre-authorization language');
}

// NEGATIVES — must return false
{
  const subj = 'New Patient Referral — intake attached';
  const body = 'Please see the attached intake forms for our new patient referral. DOB: 05/12/1988.';
  assert(!isRecordsOrBillingRequest_(subj, body), 'negative: plain new referral');
}
{
  const subj = 'Airgas Invoice Overdue';
  const body = 'Your Airgas invoice is overdue. Please remit payment at your earliest convenience.';
  assert(!isRecordsOrBillingRequest_(subj, body), 'negative: vendor invoice (Airgas)');
}
{
  const subj = 'Quick check-in';
  const body = 'Thanks for the call yesterday, talk soon!';
  assert(!isRecordsOrBillingRequest_(subj, body), 'negative: generic greeting / no patient language');
}

console.log('\n=== inbound patient report ===');

// POSITIVES -- must return true
{
  const subj = 'EMG report for the patient enclosed';
  const body = 'Please find the EMG report enclosed for your review.';
  assert(isInboundPatientReport_(subj, body), 'inbound report positive: EMG report enclosed');
}
{
  const subj = 'MRI results attached';
  const body = 'The MRI results for the above patient are attached for your records.';
  assert(isInboundPatientReport_(subj, body), 'inbound report positive: MRI results attached');
}
{
  const subj = 'Operative note - left knee';
  const body = 'Please see the attached operative note for the left knee procedure performed on 06/10/2026.';
  assert(isInboundPatientReport_(subj, body), 'inbound report positive: operative note');
}
{
  const subj = 'Discharge summary';
  const body = 'Attached is the discharge summary from the hospital stay. Impression: improving.';
  assert(isInboundPatientReport_(subj, body), 'inbound report positive: discharge summary');
}
{
  const subj = 'Lab results / pathology report';
  const body = 'Lab results and pathology report for patient are enclosed. Findings noted below.';
  assert(isInboundPatientReport_(subj, body), 'inbound report positive: lab results / pathology report');
}

// NEGATIVES -- must return false
{
  const subj = 'New patient referral, please see attached intake';
  const body = 'We are referring a new patient for pain management evaluation. DOB: 03/12/1985.';
  assert(!isInboundPatientReport_(subj, body), 'inbound report negative: new patient referral');
}
{
  const subj = 'Medical records request';
  const body = 'Please send us a copy of the medical records for our client.';
  assert(!isInboundPatientReport_(subj, body), 'inbound report negative: records request (not a report)');
}
{
  const subj = 'Your Airgas invoice is overdue';
  const body = 'Your Airgas invoice is overdue. Please remit payment at your earliest convenience.';
  assert(!isInboundPatientReport_(subj, body), 'inbound report negative: vendor invoice');
}
{
  const subj = 'Thanks, talk soon';
  const body = 'Great catching up. Talk soon!';
  assert(!isInboundPatientReport_(subj, body), 'inbound report negative: generic greeting');
}

console.log('\n=== isLikelyEmail_ ===');

// Positives
assert(isLikelyEmail_('a@b.com'),              'positive: a@b.com');
assert(isLikelyEmail_('john.doe@law-firm.org'), 'positive: john.doe@law-firm.org');

// Negatives
assert(!isLikelyEmail_(''),                    'negative: empty string');
assert(!isLikelyEmail_('not an email'),        'negative: plain text');
assert(!isLikelyEmail_('a@b'),                 'negative: no dot in domain');
assert(!isLikelyEmail_('a b@c.com'),           'negative: space before @');

console.log('\n=== normalizeDobForFolderMatch_ ===');

assert(normalizeDobForFolderMatch_('03-04-2006') === '03042006', 'dash DOB normalized');
assert(normalizeDobForFolderMatch_('DOB 03/04/2006') === '03042006', 'label/slash DOB normalized');
assert(normalizeDobForFolderMatch_('') === '', 'empty DOB normalized');

console.log('\n=== buildRecordsReplyBody_ ===');

{
  // Returns object with non-empty .plain and .html
  const r = buildRecordsReplyBody_('Jose Cartagena', '04-02-1968', 'Records', 5);
  assert(typeof r === 'object' && r !== null, 'returns an object');
  assert(typeof r.plain === 'string' && r.plain.length > 0, '.plain is non-empty string');
  assert(typeof r.html  === 'string' && r.html.length  > 0, '.html is non-empty string');
}
{
  // Full records case: name, DOB, doc count, authorization line all present
  const r = buildRecordsReplyBody_('Jose Cartagena', '04-02-1968', 'Records', 5);
  assert(r.plain.indexOf('Jose Cartagena') !== -1, 'Records/5 docs: .plain contains patient name');
  assert(r.plain.indexOf('04-02-1968')    !== -1, 'Records/5 docs: .plain contains DOB');
  assert(r.plain.indexOf('5 document')    !== -1, 'Records/5 docs: .plain contains "5 document"');
  assert(r.plain.indexOf('authorization') !== -1, 'Records/5 docs: .plain contains "authorization"');
}
{
  // docCount 0: uses "locating" language, not the found-count sentence
  const r = buildRecordsReplyBody_('Test Patient', '01-01-1990', 'Records', 0);
  assert(r.plain.indexOf('locating') !== -1, 'docCount 0: .plain contains "locating"');
}
{
  // Billing/Insurance reqType: billing line present, medical-records release line absent
  const r = buildRecordsReplyBody_('Test Patient', '', 'Billing/Insurance', 3);
  assert(r.plain.toLowerCase().indexOf('billing') !== -1, 'Billing/Insurance: .plain contains "billing"');
  assert(r.plain.indexOf('We will release the requested medical records') === -1,
    'Billing/Insurance: .plain does NOT contain medical records release line');
}
{
  // Sanity: no HTTP links in plain text
  const r = buildRecordsReplyBody_('Any Patient', '06-15-1975', 'Records', 2);
  assert(r.plain.indexOf('http') === -1, 'no "http" links in .plain');
}

// ---------------------------------------------------------------------------
// === Sway report ===
// ---------------------------------------------------------------------------

console.log('\n=== Sway report ===');

// isSwayReportName_ - positives
assert(isSwayReportName_('Sway_2701977_2026-06-22--15-13') === true,  'Sway_ prefix matches');
assert(isSwayReportName_('Sway-123.pdf')                   === true,  'Sway- prefix matches');
assert(isSwayReportName_('Sway 2026-06-22 report')         === true,  'Sway<space> prefix matches');

// isSwayReportName_ - negatives
assert(isSwayReportName_('New Patient Referral')           === false, 'non-sway subject: false');
assert(isSwayReportName_('')                               === false, 'empty string: false');

// isObviousReferralSubject_
assert(isObviousReferralSubject_('REFERRAL NICHOL MCCLEARY') === true,  'obvious referral - bare REFERRAL prefix');
assert(isObviousReferralSubject_('New Patient Referral: Smith') === true, 'obvious referral - New Patient Referral');
assert(isObviousReferralSubject_('Re: Referral - Jones') === true,       'obvious referral - Re: Referral');
assert(isObviousReferralSubject_('Records Request - Jose Cartagena') === false, 'obvious referral - records request is not');
assert(isObviousReferralSubject_('Your referral was received') === false, 'obvious referral - mid-sentence is not');
assert(isObviousReferralSubject_('') === false,                          'obvious referral - empty');

// parseSwayPatient_ - high confidence from realistic clinical report OCR text
{
  var t = 'CLINICAL REPORT TEST DATE: 06/22/2026\nPATIENT NAME\nJAWSHAWN ALLEN\nDOB: 3/4/2006 Age: 20 Height: 6\'5"';
  var p = parseSwayPatient_(t);
  assert(p.firstName.toLowerCase() === 'jawshawn', 'Sway OCR: firstName is jawshawn');
  assert(p.lastName.toLowerCase()  === 'allen',    'Sway OCR: lastName is allen');
  assert(p.dob                     === '03-04-2006','Sway OCR: dob is 03-04-2006');
  assert(p.confidence              === 'high',     'Sway OCR: confidence is high');
}

// parseSwayPatient_ - empty text -> confidence none
{
  var pEmpty = parseSwayPatient_('');
  assert(pEmpty.confidence === 'none', 'parseSwayPatient_ empty text -> none');
}

// ---------------------------------------------------------------------------
// isOcrableAttachment_ and looksLikeReferralDoc_
// ---------------------------------------------------------------------------

console.log('\n=== isOcrableAttachment_ ===');
assert(isOcrableAttachment_('application/pdf') === true,  'PDF content-type is OCR-able');
assert(isOcrableAttachment_('image/jpeg')      === true,  'image/jpeg is OCR-able');
assert(isOcrableAttachment_('image/png')       === true,  'image/png is OCR-able');
assert(isOcrableAttachment_('text/plain')      === false, 'text/plain is not OCR-able');
assert(isOcrableAttachment_('')               === false, 'empty string is not OCR-able');

console.log('\n=== looksLikeReferralDoc_ ===');
assert(looksLikeReferralDoc_('... PATIENT REFERRAL ... Referral Queue ID: 1202208959 ...') === true,  'patient referral phrase matches');
assert(looksLikeReferralDoc_('Date of Injury: 04/30/2026')                                === true,  'date of injury phrase matches');
assert(looksLikeReferralDoc_('Your monthly invoice is attached')                          === false, 'invoice text does not match');
assert(looksLikeReferralDoc_('')                                                           === false, 'empty string does not match');

// ---------------------------------------------------------------------------
// === isSalesSolicitation_ ===
// ---------------------------------------------------------------------------

console.log('\n=== isSalesSolicitation_ ===');

{
  const subj = 'Review of Atlantic Pain and Wellness Ins | No Deductions?';
  const body = "let's chat about pre-tax deductions and retirement plan setup; promotions and waived fees this week";
  assert(isSalesSolicitation_(subj, body) === true,
    'sales solicitation positive: 401k/retirement/pre-tax + promotions/waived fees');
}
{
  const subj = 'Patient Referral - Jane Doe';
  const body = 'DOB 01/01/1990, MRI attached';
  assert(isSalesSolicitation_(subj, body) === false,
    'sales solicitation negative: patient referral with MRI/DOB');
}
{
  const subj = 'Records request for John Smith';
  const body = 'please send medical records';
  assert(isSalesSolicitation_(subj, body) === false,
    'sales solicitation negative: records request (medical records signal)');
}

// ---------------------------------------------------------------------------
// === extractRequestedPatientName_ ===
// ---------------------------------------------------------------------------

console.log('\n=== extractRequestedPatientName_ ===');

{
  const r = extractRequestedPatientName_("Re: EMG Report & Bill", "Could you please forward me a copy of Ms. Zambrana's EMG report?");
  assert(r.lastName === 'Zambrana',
    'extractRequestedPatientName_: Ms. Zambrana -> lastName Zambrana');
}
{
  const r = extractRequestedPatientName_('records', "copy of John Smith's records");
  assert(r.firstName === 'John' && r.lastName === 'Smith',
    "extractRequestedPatientName_: John Smith's records -> firstName John, lastName Smith");
}

// ---------------------------------------------------------------------------
// === isRecordsOrBillingRequest_ (new patterns) ===
// ---------------------------------------------------------------------------

console.log('\n=== isRecordsOrBillingRequest_ (new patterns) ===');

{
  assert(isRecordsOrBillingRequest_('Re: EMG Report & Bill', 'forward me a copy of Ms. Zambrana\'s EMG report') === true,
    'isRecordsOrBillingRequest_: "forward me a copy of...EMG report" matches');
}

// ---------------------------------------------------------------------------
// === buildRecordsReplyBody_ (with requestedDoc) ===
// ---------------------------------------------------------------------------

console.log('\n=== buildRecordsReplyBody_ (requestedDoc args) ===');

{
  const r = buildRecordsReplyBody_('Ana Zambrana', '', 'Records', 3, 'EMG', true);
  assert(r.plain.indexOf('EMG') !== -1,
    'buildRecordsReplyBody_ with EMG on file: plain contains EMG');
  assert(r.plain.indexOf('authorization') !== -1,
    'buildRecordsReplyBody_ with EMG on file: plain contains "authorization"');
}
{
  const r = buildRecordsReplyBody_('Ana Zambrana', '', 'Records', 3, 'EMG', false);
  assert(r.plain.indexOf('EMG') !== -1,
    'buildRecordsReplyBody_ EMG not on file: plain contains EMG');
  assert(r.plain.toLowerCase().indexOf('locating') !== -1,
    'buildRecordsReplyBody_ EMG not on file: plain contains "locating"');
}
{
  // Back-compat: 4-arg call must still work
  const r = buildRecordsReplyBody_('Jose Cartagena', '04-02-1968', 'Records', 5);
  assert(r.plain.indexOf('Jose Cartagena') !== -1,
    'buildRecordsReplyBody_ 4-arg back-compat: name present');
  assert(r.plain.indexOf('authorization') !== -1,
    'buildRecordsReplyBody_ 4-arg back-compat: authorization line present');
}

// ---------------------------------------------------------------------------
// === isTerminalLedgerStatus_ ===
// ---------------------------------------------------------------------------

console.log('\n=== isTerminalLedgerStatus_ ===');

['records', 'review', 'error', 'report-filed', 'stale-claim', 'done', 'ignored'].forEach((s) => {
  assert(isTerminalLedgerStatus_(s) === true, `isTerminalLedgerStatus_('${s}') -> true`);
});
[['claimed', false], ['retry', false], ['', false], [undefined, false]].forEach(([s, expected]) => {
  assert(isTerminalLedgerStatus_(s) === expected, `isTerminalLedgerStatus_(${JSON.stringify(s)}) -> ${expected}`);
});

// ---------------------------------------------------------------------------
// === isIntakeAlertSubject_ ===
// ---------------------------------------------------------------------------

console.log('\n=== isIntakeAlertSubject_ ===');

assert(isIntakeAlertSubject_('[Intake Alert] New referral: X — call to book') === true,
  'plain alert subject -> true');
assert(isIntakeAlertSubject_('Re: [Intake Alert] New referral: X — call to book') === true,
  'Re: prefix -> true');
assert(isIntakeAlertSubject_('Fwd: [Intake Alert] New referral: X — call to book') === true,
  'Fwd: prefix -> true');
assert(isIntakeAlertSubject_('[Intake] 3 item(s) need review') === true,
  'digest subject -> true');
assert(isIntakeAlertSubject_('New Patient Referral') === false,
  'ordinary referral subject -> false');

// ---------------------------------------------------------------------------
// === buildReferralAlertBody_ ===
// ---------------------------------------------------------------------------

console.log('\n=== buildReferralAlertBody_ ===');

{
  const r = buildReferralAlertBody_('Jose Cartagena', '04-02-1968', 'Workers Comp', false,
    '555-123-4567', 'Dr. Smith', 'https://drive.google.com/drive/folders/abc123');
  assert(r.subject.indexOf('[Intake Alert] ') === 0, 'subject starts with "[Intake Alert] "');
  assert(r.plain.indexOf('https://drive.google.com/drive/folders/abc123') !== -1,
    'plain body contains the folder URL');
  assert(r.plain.indexOf('undefined') === -1, 'plain body never contains "undefined"');
}
{
  // Missing fields must render '—', never invented values or "undefined"
  const r = buildReferralAlertBody_('', '', '', false, '', '', '');
  assert(r.subject.indexOf('[Intake Alert] ') === 0, 'subject prefix present even with all fields missing');
  assert(r.plain.indexOf('undefined') === -1, 'plain body never contains "undefined" when fields are missing');
  assert(r.plain.indexOf('—') !== -1, 'plain body renders "—" for missing fields');
}
{
  const r = buildReferralAlertBody_('Ana Zambrana', '01-01-1990', 'MVA', true, '555-000-1111', 'Self', 'https://drive.google.com/x');
  assert(r.plain.toLowerCase().indexOf('head injury') !== -1, 'HEAD INJURY flag present when isHeadInjury is true');
}

// ---------------------------------------------------------------------------
// === matchesPatientFolderName_ ===
// ---------------------------------------------------------------------------

console.log('\n=== matchesPatientFolderName_ ===');

assert(matchesPatientFolderName_('Asad Grant (DOB 01-11-1999)', 'Asad', 'Grant', '01-11-1999') === true,
  'First Last (DOB dash) matches same DOB dash');
assert(matchesPatientFolderName_('GRANT, ASAD - DOB 01/11/1999', 'Asad', 'Grant', '01-11-1999') === true,
  'LAST, FIRST - DOB slash matches First Last dash dob (cross-format)');
assert(matchesPatientFolderName_('grant, asad - dob 01/11/1999', 'ASAD', 'GRANT', '01/11/1999') === true,
  'case-insensitive both sides');
assert(matchesPatientFolderName_('Asad Grant', 'Asad', 'Grant', '') === true,
  'name match suffices when neither side has a DOB');
assert(matchesPatientFolderName_('Asad Grant (DOB 01-11-1999)', 'Asad', 'Grant', '') === true,
  'folder has DOB but supplied dob is empty -> name match suffices');
assert(matchesPatientFolderName_('Asad Grant (DOB 05-06-2000)', 'Asad', 'Grant', '01-11-1999') === false,
  'mismatched DOB -> false');
assert(matchesPatientFolderName_('Someone Else (DOB 01-11-1999)', 'Asad', 'Grant', '01-11-1999') === false,
  'mismatched name -> false');
assert(matchesPatientFolderName_('Grantham, Asadollah - DOB 01-11-1999', 'Asad', 'Grant', '01-11-1999') === false,
  'prefix must be exact token, not substring of a longer surname');

// ---------------------------------------------------------------------------
// === isIgnoredSender_ / isPartnerBillingSender_ ===
// ---------------------------------------------------------------------------

console.log('\n=== isIgnoredSender_ / isPartnerBillingSender_ ===');

assert(isIgnoredSender_('Invoice+Statements@Mail.Anthropic.com', 'invoice+statements@mail.anthropic.com,alerts@tdbank.com') === true,
  'isIgnoredSender_ case-insensitive exact match -> true');
assert(isIgnoredSender_('someone@notondlist.com', 'invoice+statements@mail.anthropic.com,alerts@tdbank.com') === false,
  'isIgnoredSender_ sender not on list -> false');
assert(isIgnoredSender_('billing@example.com', '') === false,
  'isIgnoredSender_ empty list -> false');

assert(isPartnerBillingSender_('Jane Doe <jane@mdmanage.com>', 'mdmanage.com,srm-inc.com') === true,
  'isPartnerBillingSender_ exact domain match -> true');
assert(isPartnerBillingSender_('jane@billing.mdmanage.com', 'mdmanage.com,srm-inc.com') === true,
  'isPartnerBillingSender_ subdomain match -> true');
assert(isPartnerBillingSender_('jane@notmdmanage.com', 'mdmanage.com') === false,
  'isPartnerBillingSender_ lookalike domain (no dot boundary) -> false');
assert(isPartnerBillingSender_('jane@othercompany.com', 'mdmanage.com,srm-inc.com') === false,
  'isPartnerBillingSender_ unrelated domain -> false');

// ---------------------------------------------------------------------------
// === parseFaxNotification_ ===
// ---------------------------------------------------------------------------

console.log('\n=== parseFaxNotification_ ===');

{
  const r = parseFaxNotification_('New Fax Message from (469) 543-6401 on 07/01/2026 6:03 PM', 'You have a new fax.\nPages: 5\n');
  assert(r !== null, 'fax subject parses -> non-null');
  assert(r.fromNumber === '(469) 543-6401', 'extracts fromNumber');
  assert(r.pages === 5, 'extracts pages');
}
{
  const r = parseFaxNotification_('New Fax Message from (469) 543-6401 on 07/01/2026', '');
  assert(r !== null && r.pages === 0, 'missing Pages line -> pages 0');
}
assert(parseFaxNotification_('New Voice Message from (469) 543-6401 on 07/01/2026 6:03 PM', '') === null,
  'voicemail subject rejected by fax regex -> null');
assert(parseFaxNotification_('Re: Your invoice is ready', '') === null,
  'non-RingCentral subject -> null');

// ---------------------------------------------------------------------------
// === countDistinctDobs_ ===
// ---------------------------------------------------------------------------

console.log('\n=== countDistinctDobs_ ===');

assert(countDistinctDobs_('No dates of birth here at all.') === 0, 'no DOBs -> 0');
assert(countDistinctDobs_('Patient DOB: 03/04/2006, seen for follow-up.') === 1, 'single DOB -> 1');
assert(countDistinctDobs_('Patient 1 DOB: 03/04/2006\nPatient 2 DOB: 11-22-1985\n') === 2,
  'two distinct DOBs -> 2');
assert(countDistinctDobs_('DOB: 03/04/2006 ... later in doc D.O.B. 03-04-2006') === 1,
  'same DOB repeated in slash and dash formats -> counts once');

// ---------------------------------------------------------------------------
// === isBookingRequest_ ===
// ---------------------------------------------------------------------------

console.log('\n=== isBookingRequest_ ===');

assert(isBookingRequest_('Harun Omar Sahin - REQ FOR APPOINTMENT', '') === true,
  '"REQ FOR APPOINTMENT" subject -> true');
assert(isBookingRequest_('Scheduling', 'can we schedule the patient') === true,
  '"can we schedule the patient" -> true');
assert(isBookingRequest_('REQ for Medical Records & Billing', 'please send records') === false,
  'records/billing keeps priority (excluded) -> false');
assert(isBookingRequest_('Invoice', 'Your invoice is past due') === false,
  'plain invoice -> false');

// ---------------------------------------------------------------------------
// === isOwnAddress_ ===
// ---------------------------------------------------------------------------

console.log('\n=== isOwnAddress_ ===');

const CLINIC_ADDRS = 'mainlinesurgery@gmail.com,mainlinepain@gmail.com,mainsurgical@gmail.com';
assert(isOwnAddress_('mainlinepain@gmail.com', CLINIC_ADDRS) === true,
  'own address (bare) -> true');
assert(isOwnAddress_('Dr <mainlinepain@gmail.com>', CLINIC_ADDRS) === true,
  'own address (display-name form) -> true');
assert(isOwnAddress_('someone@foreignfirm.com', CLINIC_ADDRS) === false,
  'foreign address -> false');

// ---------------------------------------------------------------------------
// === buildBookingAlertBody_ ===
// ---------------------------------------------------------------------------

console.log('\n=== buildBookingAlertBody_ ===');

{
  const r = buildBookingAlertBody_('Harun Omar Sahin', 'attorney@example.com', '', '', 'REQ FOR APPOINTMENT');
  assert(r.subject.indexOf('[Intake Alert] ') === 0, 'subject starts with [Intake Alert] ');
  assert(r.plain.indexOf('undefined') === -1, 'plain body never contains "undefined"');
  assert(r.plain.indexOf('—') !== -1, 'missing phone/folder render em-dash fallback');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n${'─'.repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  console.error('TEST SUITE FAILED');
  process.exit(1);
} else {
  console.log('TEST SUITE PASSED');
  process.exit(0);
}

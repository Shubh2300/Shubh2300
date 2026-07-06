/**
 * Code.js - Core Patient Intake Orchestrator & Pipeline
 *
 * DEPLOYMENT: After editing this file or Config.js, re-paste both into the Apps Script editor
 * and save. Stale deployments use old "Patient Intake/..." labels and lack the vendor-invoice
 * classifier — re-paste is required for any classifier changes to take effect.
 *
 * This file coordinates the 7-step clinical workflow:
 * 1. Scans Gmail for new referral emails (with state tracking to avoid double runs).
 * 2. Uses Gemini 2.0 Flash to extract structured JSON patient details from email body and attachments.
 * 3. Dynamically generates patient folders in Google Drive.
 * 4. Fills out Google Doc clinical summaries from templates using placeholder replacements.
 * 5. Logs patient intake entries, folder, and document URLs to the central Google Sheet.
 * 6. Generates pre-populated pre-visit intake form URLs.
 * 7. dispatches or drafts professional clinical emails to patients and attorneys.
 */

/**
 * Main Orchestrator Trigger
 * This is the entry point. It should be scheduled to run on a timer trigger
 * (e.g., every hour or once daily) or triggered manually.
 */
function processNewPatientReferrals() {
  Logger.log('Starting Patient Referral Intake pipeline...');
  var config = getSystemConfig();

  if (!config.GEMINI_API_KEY) {
    Logger.log('ERROR: GEMINI_API_KEY is not defined in Settings. Please fill it in the "Settings" sheet tab.');
    return;
  }

  // 1. Scan Gmail for unlabelled referral threads
  var threads = findNewReferralThreads(config);
  Logger.log('Found ' + threads.length + ' new referral thread(s) to process.');

  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    try {
      Logger.log('Processing thread: ' + thread.getFirstMessageSubject());

      // Immediately label as "New Referral" to establish state
      applyLabelToThread(thread, config.LABEL_NEW);

      // Process the thread
      var result = processThread(thread, config);

      if (result === true) {
        applyLabelToThread(thread, config.LABEL_PROCESSED);
        removeLabelFromThread(thread, config.LABEL_NEW);
        removeLabelFromThread(thread, config.LABEL_ERROR);
        Logger.log('Successfully completed processing for thread: ' + thread.getFirstMessageSubject());
      } else if (result === 'ignored') {
        var ignoredLabel = config.LABEL_IGNORED || 'Referral/Ignored';
        applyLabelToThread(thread, ignoredLabel);
        removeLabelFromThread(thread, config.LABEL_NEW);
        removeLabelFromThread(thread, config.LABEL_ERROR);
        Logger.log('Thread ignored (not a patient referral): ' + thread.getFirstMessageSubject());
      } else if (result === 'retry') {
        // Transient failure (Gemini quota/network). Leave the thread UNLABELED so it's
        // retried next run, and STOP the batch — further calls will fail the same way.
        removeLabelFromThread(thread, config.LABEL_NEW);
        Logger.log('Transient quota/network failure — leaving for retry next run and stopping this batch.');
        break;
      } else {
        applyLabelToThread(thread, config.LABEL_ERROR); // If processing failed, move to Error bin
        removeLabelFromThread(thread, config.LABEL_NEW);
        Logger.log('Thread marked as Error for manual review: ' + thread.getFirstMessageSubject());
      }
    } catch (err) {
      Logger.log('CRITICAL ERROR processing thread ' + thread.getFirstMessageSubject() + ': ' + err.toString());
      applyLabelToThread(thread, config.LABEL_ERROR);
      removeLabelFromThread(thread, config.LABEL_NEW);
    }

    // Add a small 4-second delay between threads to respect the free tier rate limits (15 RPM)
    if (i < threads.length - 1) {
      Logger.log('Sleeping 4 seconds to prevent API rate-limiting...');
      Utilities.sleep(4000);
    }
  }

  Logger.log('Intake pipeline completed.');
}

/**
 * Scans Gmail inbox for emails sent by allowed senders that have not been processed.
 *
 * @param {Object} config - System configuration
 * @return {Array<GmailThread>} Matching threads
 */
function findNewReferralThreads(config) {
  // Construct search query scanning all unprocessed incoming inbox emails
  var labelQuery = '-label:' + config.LABEL_PROCESSED + ' -label:' + config.LABEL_ERROR + ' -label:' + (config.LABEL_IGNORED || 'Referral/Ignored');
  var dateQuery = config.START_DATE ? 'after:' + config.START_DATE : '';
  var inboxQuery = 'in:inbox';

  var finalQuery = [inboxQuery, labelQuery, dateQuery].filter(Boolean).join(' ');

  Logger.log('Searching Gmail with query: "' + finalQuery + '"');
  return GmailApp.search(finalQuery, 0, 10); // Process top 10 matching threads at a time
}

/**
 * Cheap detector for bulk/marketing email (newsletters, promos, blasts). These
 * carry a List-Unsubscribe or "Precedence: bulk" header and can never be a patient
 * referral — so we skip them WITHOUT spending a Gemini call (saves quota/cost).
 */
function isBulkMail(message) {
  try {
    var raw = message.getRawContent();
    return /^list-unsubscribe:/mi.test(raw) || /^precedence:\s*(bulk|list|junk)/mi.test(raw);
  } catch (e) {
    return false;
  }
}

/**
 * Cheap detector for vendor invoices / account billing notices. These are
 * emails about money WE owe a vendor (software, supplies, services) — never
 * a patient referral. Both conditions must hold so a real referral that
 * happens to mention billing is never skipped.
 */
function isVendorInvoice(message) {
  var subject = (message.getSubject() || '');
  var body = '';
  try { body = message.getPlainBody() || ''; } catch (e) {}
  var invoiceSubject = /\b(invoice|past due|payment (due|reminder|received|failed)|billing statement|receipt for|renewal notice|subscription|account (suspended|overdue|notice))\b/i.test(subject);
  var referralEvidence = /\b(referr(al|ed|ing)|date of birth|DOB|injur(y|ies)|MRI|X-?ray|therapy|work(ers)? comp|accident|patient name)\b/i.test(subject + ' ' + body.slice(0, 4000));
  return invoiceSubject && !referralEvidence;
}

/**
 * Processes an individual Gmail thread containing a referral.
 *
 * @param {GmailThread} thread - Gmail thread
 * @param {Object} config - System configuration
 * @return {boolean} True if processed successfully, false if pending manual review
 */
function processThread(thread, config) {
  var messages = thread.getMessages();
  var latestMessage = messages[messages.length - 1]; // Process the latest message in thread

  // Cheap pre-filter: skip bulk/marketing blasts without an AI call. A real patient
  // referral is never a mass mailing. Set SKIP_BULK_MAIL=false in Settings to force
  // the AI to read literally every email.
  if (config.SKIP_BULK_MAIL !== false && isBulkMail(latestMessage)) {
    Logger.log('Bulk/marketing email (List-Unsubscribe header) — auto-ignoring without an AI call.');
    return 'ignored';
  }
  if (config.SKIP_VENDOR_INVOICES !== false && isVendorInvoice(latestMessage)) {
    Logger.log('Vendor invoice/billing notice ("' + latestMessage.getSubject() + '") — auto-ignoring without an AI call.');
    return 'ignored';
  }

  var emailBody = latestMessage.getPlainBody();
  var attachments = latestMessage.getAttachments();

  // 2. Extract structured patient data using Gemini
  Logger.log('Calling Gemini Multimodal API to extract patient details...');
  var extractedData = extractPatientDataWithGemini(emailBody, attachments, config);

  if (extractedData && extractedData.__quota) {
    return 'retry';  // transient quota error — do NOT bury the thread; retry next run
  }
  if (!extractedData) {
    Logger.log('WARNING: Failed to contact Gemini API. Marking thread as pending review.');
    return false;
  }

  // Check if classified as a referral
  if (extractedData.isReferral === false) {
    Logger.log('AI classified this email as NOT a patient referral. Skipping intake processing.');
    return 'ignored';
  }

  if (!extractedData.patientName) {
    Logger.log('WARNING: AI classified this as a referral, but failed to extract a valid patient name. Marking thread as pending review.');
    return false;
  }
  var corroboration = [extractedData.patientDOB, extractedData.patientPhone, extractedData.patientEmail, extractedData.referringDoctor]
    .filter(function (v) { return v && String(v).trim(); }).length;
  if (corroboration === 0) {
    Logger.log('WARNING: Referral has a name ("' + extractedData.patientName + '") but no DOB/phone/email/referring source. Not creating records — marking for manual review.');
    return false;
  }

  Logger.log('Extracted Patient: ' + extractedData.patientName + ' (DOB: ' + extractedData.patientDOB + ')');

  // 3. Create Google Drive folder structure for the patient
  Logger.log('Generating Drive folders...');
  var folders = createPatientDriveFolder(extractedData, attachments, config);

  // 4. Fill and generate Google Doc internal templates
  var docLinks = fillDocumentTemplates(extractedData, folders.folderId, config);

  // 5. Generate pre-filled Google Form URL for patient
  var prefilledIntakeUrl = generatePrefilledFormUrl(extractedData, config.PRE_VISIT_FORM_URL);

  // 6. Log entry to central Google Sheet Dashboard
  Logger.log('Logging entry to Google Sheet Dashboard...');
  logToSpreadsheetDashboard(extractedData, folders.folderUrl, docLinks, prefilledIntakeUrl);

  // 6.25. Automatically route to the "new referrals" tab above "Seen Patients"
  Logger.log('Routing to "new referrals" tab...');
  addPatientToNewReferralsTab(extractedData, folders.folderUrl, folders.firstFileUrl, folders.firstFileName);

  // 6.5. Automatically route to specialized tracking tabs if matching clinical criteria are met
  if (extractedData.isHeadInjury) {
    Logger.log('Patient has head injury markers. Routing to "Head Injury" tab...');
    addPatientToSpecificTab('Head Injury', extractedData, folders.folderUrl);
  }
  if (extractedData.isWorkInjury) {
    Logger.log('Patient has work-related injury markers. Routing to "WC Patients" tab...');
    addPatientToSpecificTab('WC Patients', extractedData, folders.folderUrl);
  }

  // 7. Dispatch or Draft professional communications
  Logger.log('Preparing clinical emails...');
  sendPatientEmails(extractedData, prefilledIntakeUrl, config);
  sendAttorneyEmail(extractedData, config);

  // Mark email message as read
  latestMessage.markRead();

  return true;
}

/**
 * Encodes attachments and email text to query Gemini API (multimodal extraction)
 * using the structured JSON response Schema.
 *
 * @param {string} emailBody - Body of the email
 * @param {Array<GmailAttachment>} attachments - Message attachments
 * @param {Object} config - System configuration
 * @return {Object} Structured patient data
 */
function extractPatientDataWithGemini(emailBody, attachments, config) {
  var apiKey = config.GEMINI_API_KEY;
  var modelName = config.GEMINI_MODEL || 'gemini-2.0-flash';
  var url = 'https://generativelanguage.googleapis.com/v1beta/models/' + modelName + ':generateContent?key=' + apiKey;

  // Initialize parts with instructions and email body
  var parts = [
    {
      text: "You are a professional clinical administrative assistant. Analyze the following patient referral email and any attached documents (medical charts, PDFs, or referral slips) to extract structured clinical and legal details.\n\n" +
            "EMAIL BODY:\n" + emailBody + "\n\n" +
            "Please review all attachments if present and extract the data accurately. If a field is not found in the email or files, output an empty string."
    }
  ];

  // Process attachments (converting to Base64 data blocks for Gemini)
  if (attachments && attachments.length > 0) {
    Logger.log('Encoding ' + attachments.length + ' email attachment(s) for Gemini multimodal analysis...');
    for (var i = 0; i < attachments.length; i++) {
      var attachment = attachments[i];
      var contentType = (attachment.getContentType() || "").toLowerCase();

      // Only process text, PDF, and image files to avoid overhead or failures
      if (contentType.indexOf('pdf') !== -1 || contentType.indexOf('image') !== -1 || contentType.indexOf('text') !== -1) {
        var base64Data = Utilities.base64Encode(attachment.getBytes());
        parts.push({
          inlineData: {
            mimeType: contentType,
            data: base64Data
          }
        });
        Logger.log('Attached file: ' + attachment.getName() + ' (' + contentType + ')');
      }
    }
  }

  // Define strict JSON Schema output
  var responseSchema = {
    type: "OBJECT",
    properties: {
      isReferral: {
        type: "BOOLEAN",
        description: "Set to TRUE only if this email refers a PATIENT to the clinic for care: patient referral, medical clearance request, surgical coordination, physical therapy referral, or clinical intake. Set to FALSE for anything else, especially: vendor invoices or bills for software/services/supplies the clinic itself buys, payment reminders or receipts about the clinic's own account, subscription or renewal notices, administrative notices, advertisements/spam, newsletters, personal conversation, status updates on existing patients, or patient follow-up questions. An email asking OUR practice to pay money is never a referral, even if it names our doctor."
      },
      patientName: { type: "STRING", description: "Full name of the patient (capitalize correctly). Output empty string if not a referral." },
      patientDOB: { type: "STRING", description: "Date of Birth (YYYY-MM-DD format if possible, otherwise standard written format). Output empty string if not a referral." },
      patientPhone: { type: "STRING", description: "Patient's primary phone number. Output empty string if not a referral." },
      patientEmail: { type: "STRING", description: "Patient's email address. Output empty string if not a referral." },
      attorneyName: { type: "STRING", description: "Attorney's full name, if legal counsel is involved. Output empty string if not a referral." },
      attorneyEmail: { type: "STRING", description: "Attorney's contact email address. Output empty string if not a referral." },
      accidentDate: { type: "STRING", description: "Date of the accident or injury event. Output empty string if not a referral." },
      referringDoctor: { type: "STRING", description: "Name of the referring physician, medical practitioner, or clinic coordinator. Output empty string if not a referral." },
      isHeadInjury: { type: "BOOLEAN", description: "Set to TRUE if there is any indication of head injury, concussion, traumatic brain injury (TBI), headaches, dizziness, or loss of consciousness. Set to FALSE if not a referral." },
      isWorkInjury: { type: "BOOLEAN", description: "Set to TRUE if the patient's injury is a work-related accident, occurred while on the job, or is a Workers' Compensation (WC) case. Set to FALSE if not a referral." },
      insurance: { type: "STRING", description: "The name of the patient's insurance company if mentioned in the email or files (e.g. Geico, State Farm, Aetna, Farmers). Output empty string if not a referral." },
      injuries: {
        type: "ARRAY",
        items: { type: "STRING" },
        description: "List of documented physical complaints and injured areas. Output empty array if not a referral."
      },
      clinicalSummary: {
        type: "STRING",
        description: "A comprehensive 1-2 paragraph clinical narrative summarizing the referral history, clinical notes, and physical examination findings suitable for our doctor's review. Output empty string if not a referral."
      },
      missingDocs: {
        type: "ARRAY",
        items: { type: "STRING" },
        description: "List of common patient registration files that appear to be missing based on current referral records. Output empty array if not a referral."
      }
    },
    required: ["isReferral", "patientName", "patientDOB", "patientPhone", "patientEmail", "isHeadInjury", "isWorkInjury", "insurance", "injuries", "clinicalSummary", "missingDocs"]
  };

  // Assemble final payload
  var payload = {
    contents: [
      {
        parts: parts
      }
    ],
    generationConfig: {
      responseMimeType: "application/json",
      responseSchema: responseSchema,
      temperature: 0.1 // Low temperature to maximize extraction accuracy
    }
  };

  var options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  var maxRetries = 3;
  var delay = 2000;
  var response = null;
  var responseCode = 0;
  var responseText = "";

  for (var attempt = 0; attempt < maxRetries; attempt++) {
    try {
      response = UrlFetchApp.fetch(url, options);
      responseCode = response.getResponseCode();
      responseText = response.getContentText();

      if (responseCode == 429) {
        Logger.log('Received status 429 (Rate Limit/Quota Exceeded) from Gemini API. Attempt ' + (attempt + 1) + ' of ' + maxRetries + '. Retrying in ' + (delay / 1000) + 's...');
        Utilities.sleep(delay);
        delay *= 2.5; // exponential backoff
        continue;
      }
      break;
    } catch (err) {
      if (attempt === maxRetries - 1) {
        Logger.log('Fetch error on final attempt: ' + err.toString());
        return null;
      }
      Logger.log('Fetch error: ' + err.toString() + '. Retrying in ' + (delay / 1000) + 's...');
      Utilities.sleep(delay);
      delay *= 2.5;
    }
  }

  try {
    if (responseCode == 429) {
      Logger.log('Gemini quota exhausted (429) after all retries — will retry on the next scheduled run.');
      return { __quota: true };
    }
    if (responseCode != 200) {
      throw new Error('Gemini API returned status ' + responseCode + ': ' + responseText);
    }

    var jsonRes = JSON.parse(responseText);
    var jsonTextOutput = jsonRes.candidates[0].content.parts[0].text;

    // Parse the actual JSON response
    return JSON.parse(jsonTextOutput);
  } catch (err) {
    Logger.log('Error parsing Gemini API response: ' + err.toString());
    return null;
  }
}

/**
 * Creates a patient-specific folder hierarchy inside the root Drive directory,
 * and uploads all email attachments.
 *
 * @param {Object} data - Extracted patient data
 * @param {Array<GmailAttachment>} attachments - Referral files
 * @param {Object} config - System configuration
 * @return {Object} Folder metadata containing folderUrl and folderId
 */
function createPatientDriveFolder(data, attachments, config) {
  var rootFolderId = config.ROOT_DRIVE_FOLDER_ID;
  var rootFolder;

  if (rootFolderId) {
    rootFolder = DriveApp.getFolderById(rootFolderId);
  } else {
    // If not configured, default to a root "Patient Intakes" folder in user's Drive
    var folders = DriveApp.getFoldersByName('Patient Intakes');
    if (folders.hasNext()) {
      rootFolder = folders.next();
    } else {
      rootFolder = DriveApp.createFolder('Patient Intakes');
    }
  }

  // Format patient folder name: "LastName, FirstName - DOB YYYY-MM-DD"
  var nameParts = data.patientName.split(' ');
  var folderName = '';
  if (nameParts.length > 1) {
    var lastName = nameParts[nameParts.length - 1];
    var firstNames = nameParts.slice(0, nameParts.length - 1).join(' ');
    folderName = lastName + ', ' + firstNames;
  } else {
    folderName = data.patientName;
  }

  if (data.patientDOB) {
    folderName += ' - DOB ' + data.patientDOB;
  }

  // Search for an existing patient folder in Google Drive
  var folders = rootFolder.getFoldersByName(folderName);
  var patientFolder;
  if (folders.hasNext()) {
    patientFolder = folders.next();
    Logger.log('Found existing patient folder in Google Drive: "' + folderName + '"');
  } else {
    patientFolder = rootFolder.createFolder(folderName);
    Logger.log('Created new patient folder in Google Drive: "' + folderName + '"');
  }

  var patientFolderId = patientFolder.getId();
  var patientFolderUrl = patientFolder.getUrl();

  // Create or retrieve standard subfolders
  var recordsFolder;
  var recordsSubfolders = patientFolder.getFoldersByName('Referral & Medical Records');
  if (recordsSubfolders.hasNext()) {
    recordsFolder = recordsSubfolders.next();
  } else {
    recordsFolder = patientFolder.createFolder('Referral & Medical Records');
  }

  var formsFolder;
  var formsSubfolders = patientFolder.getFoldersByName('Internal Forms');
  if (formsSubfolders.hasNext()) {
    formsFolder = formsSubfolders.next();
  } else {
    formsFolder = patientFolder.createFolder('Internal Forms');
  }

  var firstFileUrl = '';
  var firstFileName = '';

  // Save original referral attachments
  if (attachments && attachments.length > 0) {
    for (var i = 0; i < attachments.length; i++) {
      var attachment = attachments[i];
      // Clean and prefix filename
      var sanitizedFilename = data.patientName.replace(/\s+/g, '_') + '_' + attachment.getName();

      // Prevent duplicate attachment uploads by checking if file already exists
      var existingFiles = recordsFolder.getFilesByName(sanitizedFilename);
      var fileObj;
      if (!existingFiles.hasNext()) {
        fileObj = recordsFolder.createFile(attachment);
        fileObj.setName(sanitizedFilename);
      } else {
        fileObj = existingFiles.next();
      }

      if (i === 0) {
        firstFileUrl = fileObj.getUrl();
        firstFileName = fileObj.getName();
      }
    }
    Logger.log('Processed email attachment(s) and synced to the clinical records subfolder.');
  }

  return {
    folderId: patientFolderId,
    folderUrl: patientFolderUrl,
    firstFileUrl: firstFileUrl,
    firstFileName: firstFileName
  };
}

/**
 * Copies clinical templates, fills in placeholders with patient data,
 * and saves completed reports.
 *
 * @param {Object} data - Patient data
 * @param {string} patientFolderId - Parent folder ID of the patient
 * @param {Object} config - System configuration
 * @return {Object} Object containing generated doc URLs
 */
function fillDocumentTemplates(data, patientFolderId, config) {
  var docLinks = {
    caseSummaryUrl: '',
    patientIntakeUrl: '',
    feeTemplateUrl: '',
    workersCompUrl: '',
    headInjuryUrl: ''
  };

  var parentFolder = DriveApp.getFolderById(patientFolderId);
  var currentDate = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM/dd/yyyy');

  // 1. DYNAMICALLY GENERATE Case Summary Doc (No template needed!)
  try {
    var summaryDocName = data.patientName + ' - Clinical Case Summary';
    var doc = DocumentApp.create(summaryDocName);
    var docFile = DriveApp.getFileById(doc.getId());
    docFile.moveTo(parentFolder);

    var body = doc.getBody();

    // Style Title
    var title = body.appendParagraph("CLINICAL CASE SUMMARY");
    title.setHeading(DocumentApp.ParagraphHeading.TITLE).setAlignment(DocumentApp.HorizontalAlignment.CENTER);
    title.editAsText().setFontFamily("Calibri").setBold(true);

    // Add Metadata Title
    var metaPara = body.appendParagraph("Date Prepared: " + currentDate);
    metaPara.setAlignment(DocumentApp.HorizontalAlignment.RIGHT);
    metaPara.editAsText().setFontSize(10).setItalic(true);

    var section1 = body.appendParagraph("Patient Demographics & Medical Referral Details");
    section1.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    section1.editAsText().setForegroundColor("#00796B").setBold(true);

    var pName = body.appendParagraph("Patient Name: " + data.patientName);
    pName.editAsText().setBold(true);
    body.appendParagraph("Date of Birth: " + (data.patientDOB || 'N/A') + " | Phone: " + (data.patientPhone || 'N/A') + " | Email: " + (data.patientEmail || 'N/A'));
    body.appendParagraph("Referring Provider: " + (data.referringDoctor || 'N/A') + " | Accident Date: " + (data.accidentDate || 'N/A'));
    body.appendParagraph("Is Concussion/Head Injury: " + (data.isHeadInjury ? "Yes" : "No") + " | Is Work Accident: " + (data.isWorkInjury ? "Yes" : "No"));
    body.appendParagraph("Legal Counsel: " + (data.attorneyName || 'N/A') + " (" + (data.attorneyEmail || 'N/A') + ")");

    body.appendHorizontalRule();

    // Physical Complaints & Injuries
    var section2 = body.appendParagraph("Physical Complaints & Diagnoses");
    section2.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    section2.editAsText().setForegroundColor("#00796B").setBold(true);

    if (data.injuries && data.injuries.length > 0) {
      for (var i = 0; i < data.injuries.length; i++) {
        var injPara = body.appendParagraph("• " + data.injuries[i]);
        injPara.editAsText().setFontSize(11);
      }
    } else {
      body.appendParagraph("No specific injuries or diagnoses documented.");
    }

    body.appendHorizontalRule();

    // Narrative Clinical Reconstruction
    var section3 = body.appendParagraph("Narrative Clinical Reconstruction");
    section3.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    section3.editAsText().setForegroundColor("#00796B").setBold(true);

    var narrative = body.appendParagraph(data.clinicalSummary || "No clinical narrative available.");
    narrative.editAsText().setFontSize(11);
    narrative.setLineSpacing(1.15);

    doc.saveAndClose();
    docLinks.caseSummaryUrl = docFile.getUrl();
    Logger.log('Generated brand-new Case Summary Document from Gemini: ' + docFile.getUrl());
  } catch (err) {
    Logger.log('Error creating Case Summary Document: ' + err.toString());
  }

  // 2. Process "Patient Intake Form" Template
  var intakeTemplateId = config.PATIENT_INTAKE_TEMPLATE_ID;
  if (intakeTemplateId) {
    try {
      var templateFile = DriveApp.getFileById(intakeTemplateId);
      var newDocName = data.patientName + ' - Patient Intake Form';
      var copiedFile = templateFile.makeCopy(newDocName, parentFolder);
      var doc = DocumentApp.openById(copiedFile.getId());
      var body = doc.getBody();

      // Perform placeholder replacement
      body.replaceText('{{PatientName}}', data.patientName);
      body.replaceText('{{DOB}}', data.patientDOB || 'N/A');
      body.replaceText('{{Phone}}', data.patientPhone || 'N/A');
      body.replaceText('{{Email}}', data.patientEmail || 'N/A');
      body.replaceText('{{ReferringDoctor}}', data.referringDoctor || 'N/A');
      body.replaceText('{{AccidentDate}}', data.accidentDate || 'N/A');
      body.replaceText('{{AttorneyName}}', data.attorneyName || 'N/A');
      body.replaceText('{{Injuries}}', (data.injuries && data.injuries.length > 0) ? data.injuries.join(', ') : 'None documented');
      body.replaceText('{{CurrentDate}}', currentDate);

      doc.saveAndClose();
      docLinks.patientIntakeUrl = copiedFile.getUrl();
      Logger.log('Generated Patient Intake Form: ' + copiedFile.getUrl());
    } catch (err) {
      Logger.log('Error creating Patient Intake Document: ' + err.toString());
    }
  } else {
    Logger.log('WARNING: PATIENT_INTAKE_TEMPLATE_ID not configured in Settings.');
  }

  // 3. Process "Fee Template"
  var feeTemplateId = config.FEE_TEMPLATE_ID;
  if (feeTemplateId) {
    try {
      var templateFile = DriveApp.getFileById(feeTemplateId);
      var newDocName = data.patientName + ' - Fee Template';
      var copiedFile = templateFile.makeCopy(newDocName, parentFolder);
      var doc = DocumentApp.openById(copiedFile.getId());
      var body = doc.getBody();

      body.replaceText('{{PatientName}}', data.patientName);
      body.replaceText('{{DOB}}', data.patientDOB || 'N/A');
      body.replaceText('{{Phone}}', data.patientPhone || 'N/A');
      body.replaceText('{{Email}}', data.patientEmail || 'N/A');
      body.replaceText('{{CurrentDate}}', currentDate);

      doc.saveAndClose();
      docLinks.feeTemplateUrl = copiedFile.getUrl();
      Logger.log('Generated Fee Document: ' + copiedFile.getUrl());
    } catch (err) {
      Logger.log('Error creating Fee Document: ' + err.toString());
    }
  } else {
    Logger.log('WARNING: FEE_TEMPLATE_ID not configured in Settings.');
  }

  // 4. Process "Workers Comp Form" (WC Form)
  // Smart check: Only generate if patient has a work-related injury
  var wcTemplateId = config.WORKERS_COMP_TEMPLATE_ID;
  if (wcTemplateId) {
    if (data.isWorkInjury) {
      try {
        var templateFile = DriveApp.getFileById(wcTemplateId);
        var newDocName = data.patientName + ' - Workers Comp Form';
        var copiedFile = templateFile.makeCopy(newDocName, parentFolder);
        var doc = DocumentApp.openById(copiedFile.getId());
        var body = doc.getBody();

        body.replaceText('{{PatientName}}', data.patientName);
        body.replaceText('{{DOB}}', data.patientDOB || 'N/A');
        body.replaceText('{{Phone}}', data.patientPhone || 'N/A');
        body.replaceText('{{Email}}', data.patientEmail || 'N/A');
        body.replaceText('{{ReferringDoctor}}', data.referringDoctor || 'N/A');
        body.replaceText('{{AccidentDate}}', data.accidentDate || 'N/A');
        body.replaceText('{{Injuries}}', (data.injuries && data.injuries.length > 0) ? data.injuries.join(', ') : 'None documented');
        body.replaceText('{{CurrentDate}}', currentDate);

        doc.saveAndClose();
        docLinks.workersCompUrl = copiedFile.getUrl();
        Logger.log('Generated Workers Comp Form: ' + copiedFile.getUrl());
      } catch (err) {
        Logger.log('Error creating Workers Comp Document: ' + err.toString());
      }
    } else {
      docLinks.workersCompUrl = 'N/A (Non-Work Injury)';
      Logger.log('Patient did not present work-related injury markers. Skipped Workers Comp Form.');
    }
  } else {
    Logger.log('INFO: WORKERS_COMP_TEMPLATE_ID not configured in Settings.');
  }

  // 5. Process "Head Injury Report"
  // Smart check: Only generate if patient has a head/concussion injury
  var headInjuryTemplateId = config.HEAD_INJURY_TEMPLATE_ID;
  if (headInjuryTemplateId) {
    if (data.isHeadInjury || data.clinicalSummary.toLowerCase().indexOf('concussion') !== -1 || data.clinicalSummary.toLowerCase().indexOf('headache') !== -1) {
      try {
        var templateFile = DriveApp.getFileById(headInjuryTemplateId);
        var newDocName = data.patientName + ' - Head Injury Report';
        var copiedFile = templateFile.makeCopy(newDocName, parentFolder);
        var doc = DocumentApp.openById(copiedFile.getId());
        var body = doc.getBody();

        body.replaceText('{{PatientName}}', data.patientName);
        body.replaceText('{{DOB}}', data.patientDOB || 'N/A');
        body.replaceText('{{Phone}}', data.patientPhone || 'N/A');
        body.replaceText('{{Email}}', data.patientEmail || 'N/A');
        body.replaceText('{{ReferringDoctor}}', data.referringDoctor || 'N/A');
        body.replaceText('{{AccidentDate}}', data.accidentDate || 'N/A');
        body.replaceText('{{Injuries}}', (data.injuries && data.injuries.length > 0) ? data.injuries.join(', ') : 'None documented');
        body.replaceText('{{ClinicalSummary}}', data.clinicalSummary || 'No clinical summary available.');
        body.replaceText('{{CurrentDate}}', currentDate);

        doc.saveAndClose();
        docLinks.headInjuryUrl = copiedFile.getUrl();
        Logger.log('Generated Head Injury Report: ' + copiedFile.getUrl());
      } catch (err) {
        Logger.log('Error creating Head Injury Document: ' + err.toString());
      }
    } else {
      docLinks.headInjuryUrl = 'N/A (Non-Head Injury)';
      Logger.log('Patient did not present concussion / head trauma clinical markers. Skipped Head Injury Report.');
    }
  } else {
    Logger.log('INFO: HEAD_INJURY_TEMPLATE_ID not configured in Settings.');
  }

  return docLinks;
}

/**
 * Builds a pre-filled Google Form URL so the patient doesn't need to retype their
 * Name, Email, and Phone.
 *
 * @param {Object} data - Patient data
 * @param {string} baseUrl - Baseline Form URL
 * @return {string} Pre-filled Google Form URL
 */
function generatePrefilledFormUrl(data, baseUrl) {
  if (!baseUrl || baseUrl.indexOf('viewform') === -1) {
    return baseUrl;
  }

  // Pre-filled forms use query parameters. Because Google Form entry IDs are highly custom
  // (e.g. entry.1028392=John+Doe), users can map their entry IDs in this function or use this
  // baseline implementation. We will include instructions in the README.md on how to inspect
  // form parameters. Below we demonstrate custom parameter mappings.
  try {
    var params = [];

    // Default placeholder pre-fills (change 'entry.xxxx' keys based on your actual Form fields)
    // We include standard tags that will be easily replaced:
    params.push('entry.patient_name=' + encodeURIComponent(data.patientName));
    if (data.patientEmail) params.push('entry.patient_email=' + encodeURIComponent(data.patientEmail));
    if (data.patientPhone) params.push('entry.patient_phone=' + encodeURIComponent(data.patientPhone));
    if (data.patientDOB) params.push('entry.patient_dob=' + encodeURIComponent(data.patientDOB));

    var querySeparator = baseUrl.indexOf('?') === -1 ? '?' : '&';
    return baseUrl + querySeparator + params.join('&');
  } catch (err) {
    Logger.log('Error appending prefill parameters: ' + err.toString());
    return baseUrl;
  }
}

/**
 * Records intake entries, Google Drive folder links, and Google Doc URLs directly
 * to the "Active Intake Queue" tab in the central Google Sheet.
 *
 * @param {Object} data - Structured patient data
 * @param {string} folderUrl - Google Drive patient folder URL
 * @param {Object} docLinks - Document links (Injury Summary and Head Injury)
 * @param {string} prefilledFormUrl - Patient form link
 */
function logToSpreadsheetDashboard(data, folderUrl, docLinks, prefilledFormUrl) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) return;

    var sheet = ss.getSheetByName('Active Intake Queue');
    if (!sheet) {
      sheet = ss.insertSheet('Active Intake Queue');

      // Build Headers
      var headers = [
        'Patient Name', 'DOB', 'Phone', 'Email', 'Referring Doctor',
        'Accident Date', 'Attorney Name', 'Attorney Email', 'Is Head Injury?',
        'Is Work Injury?', 'Intake Status', 'Drive Folder Link', 'Case Summary Link',
        'Patient Intake Link', 'Fee Template Link', 'Workers Comp Link', 'Head Injury Report Link',
        'Intake Form Prefilled URL', 'Last Updated'
      ];
      sheet.getRange('A1:S1').setValues([headers]);
      sheet.getRange('A1:S1').setFontWeight('bold').setBackground('#E8F5E9').setFontColor('#1B5E20');
      sheet.setFrozenRows(1);
    }

    var lastRow = sheet.getLastRow();
    var values = sheet.getRange(2, 1, lastRow === 1 ? 1 : lastRow - 1, 2).getValues();
    var existingRowIndex = -1;

    // Check for duplicates by matching Patient Name and DOB
    if (lastRow > 1) {
      for (var i = 0; i < values.length; i++) {
        var rowName = String(values[i][0]).toLowerCase().trim();
        var rowDob = String(values[i][1]).toLowerCase().trim();

        if (rowName === data.patientName.toLowerCase().trim() && rowDob === String(data.patientDOB).toLowerCase().trim()) {
          existingRowIndex = i + 2; // Conversion to 1-indexed spreadsheet row
          break;
        }
      }
    }

    var currentDate = new Date();
    var status = (data.missingDocs && data.missingDocs.length > 0) ? 'Missing Info Requested' : 'Folder Created';

    var rowData = [
      data.patientName,
      data.patientDOB || '',
      data.patientPhone || '',
      data.patientEmail || '',
      data.referringDoctor || '',
      data.accidentDate || '',
      data.attorneyName || '',
      data.attorneyEmail || '',
      data.isHeadInjury ? 'Yes' : 'No',
      data.isWorkInjury ? 'Yes' : 'No',
      status,
      folderUrl,
      docLinks.caseSummaryUrl || '',
      docLinks.patientIntakeUrl || '',
      docLinks.feeTemplateUrl || '',
      docLinks.workersCompUrl || '',
      docLinks.headInjuryUrl || '',
      prefilledFormUrl || '',
      currentDate
    ];

    if (existingRowIndex !== -1) {
      // Update existing record
      sheet.getRange(existingRowIndex, 1, 1, rowData.length).setValues([rowData]);
      Logger.log('Updated existing record in Dashboard for: ' + data.patientName);
    } else {
      // Append new patient row
      sheet.appendRow(rowData);
      Logger.log('Appended new record in Dashboard for: ' + data.patientName);
    }

    sheet.autoResizeColumns(1, 19);
  } catch (err) {
    Logger.log('Error writing entry to Google Sheet Dashboard: ' + err.toString());
  }
}

/**
 * Automatically routes patient referrals to specialized trackers (e.g. "Head Injury" or "WC Patients")
 * using dynamic column header scanning, name-splitting, hyperlink creation, and duplicate check.
 *
 * @param {string} sheetName - Tab name (e.g. 'Head Injury' or 'WC Patients')
 * @param {Object} data - Structured patient data
 * @param {string} folderUrl - Google Drive folder URL for the patient
 */
function addPatientToSpecificTab(sheetName, data, folderUrl) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) return;

    var sheet = ss.getSheetByName(sheetName);
    if (!sheet) {
      Logger.log('WARNING: Sheet tab "' + sheetName + '" not found. Skipping auto-routing.');
      return;
    }

    // Read column headers in row 1
    var lastCol = sheet.getLastColumn();
    if (lastCol === 0) {
      Logger.log('WARNING: Sheet tab "' + sheetName + '" has no columns. Skipping.');
      return;
    }

    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];

    // Split the patient's full name into First Name and Last Name
    var nameParts = data.patientName.split(' ');
    var firstName = '';
    var lastName = '';
    if (nameParts.length > 1) {
      lastName = nameParts[nameParts.length - 1];
      firstName = nameParts.slice(0, nameParts.length - 1).join(' ');
    } else {
      firstName = data.patientName;
    }

    // Scan headers to locate important patient columns and the primary folder link column
    var firstNameColIndex = -1;
    var lastNameColIndex = -1;
    var folderColIndex = -1; // Column with folder formulas, usually column 1/A or matching folder/name/nce

    for (var i = 0; i < headers.length; i++) {
      var header = String(headers[i]).toLowerCase().trim();
      if (header.indexOf('first name') !== -1) {
        firstNameColIndex = i;
      } else if (header.indexOf('last name') !== -1) {
        lastNameColIndex = i;
      } else if (i === 0 || header.indexOf('folder') !== -1 || header.indexOf('link') !== -1 || header === 'nce' || header === 'name') {
        if (folderColIndex === -1) {
          folderColIndex = i;
        }
      }
    }

    // De-duplication check: scan sheet rows to see if this patient is already registered
    var lastRow = sheet.getLastRow();
    var isDuplicate = false;

    if (lastRow > 1) {
      var dataRange = sheet.getRange(2, 1, lastRow - 1, lastCol);
      var rowValues = dataRange.getValues();

      for (var r = 0; r < rowValues.length; r++) {
        var row = rowValues[r];
        if (firstNameColIndex !== -1 && lastNameColIndex !== -1) {
          var rowFirst = String(row[firstNameColIndex]).trim().toLowerCase();
          var rowLast = String(row[lastNameColIndex]).trim().toLowerCase();
          if (rowFirst === firstName.toLowerCase().trim() && rowLast === lastName.toLowerCase().trim()) {
            isDuplicate = true;
            break;
          }
        } else {
          // Fallback: search for patient name anywhere in first or folder column
          var colToCheck = folderColIndex !== -1 ? folderColIndex : 0;
          var cellVal = String(row[colToCheck]).trim().toLowerCase();
          if (cellVal.indexOf(data.patientName.toLowerCase().trim()) !== -1 ||
              (lastName && cellVal.indexOf(lastName.toLowerCase().trim()) !== -1 && cellVal.indexOf(firstName.toLowerCase().trim()) !== -1)) {
            isDuplicate = true;
            break;
          }
        }
      }
    }

    if (isDuplicate) {
      Logger.log('Patient "' + data.patientName + '" is already listed in "' + sheetName + '" tab. Skipping duplicate.');
      return;
    }

    // Construct the new row matching headers dynamically
    var newRow = new Array(headers.length);
    for (var i = 0; i < headers.length; i++) {
      newRow[i] = ''; // Default to blank string
    }

    for (var i = 0; i < headers.length; i++) {
      var header = String(headers[i]).toLowerCase().trim();

      // 1. Folder Link Hyperlink
      if (i === folderColIndex) {
        newRow[i] = '=HYPERLINK("' + folderUrl + '", "📁 ' + data.patientName + '")';
      }
      // 2. First Name
      else if (header.indexOf('first name') !== -1) {
        newRow[i] = firstName;
      }
      // 3. Last Name
      else if (header.indexOf('last name') !== -1) {
        newRow[i] = lastName;
      }
      // 4. Case Type
      else if (header.indexOf('case type') !== -1 || header === 'case') {
        newRow[i] = data.isWorkInjury ? 'WC' : 'MVA';
      }
      // 5. Scheduling status (dropdown matches red "Not Seen" pill)
      else if (header.indexOf('scheduled') !== -1 || header.indexOf('status') !== -1) {
        newRow[i] = 'Not Seen';
      }
      // 6. Phone
      else if (header.indexOf('phone') !== -1 || header.indexOf('contact') !== -1) {
        newRow[i] = data.patientPhone || '';
      }
      // 7. Email
      else if (header.indexOf('email') !== -1) {
        newRow[i] = data.patientEmail || '';
      }
      // 8. Referring Doctor
      else if (header.indexOf('doctor') !== -1 || header.indexOf('referring') !== -1 || header.indexOf('source') !== -1) {
        newRow[i] = data.referringDoctor || '';
      }
      // 9. Accident Date
      else if (header.indexOf('accident') !== -1 || header.indexOf('injury date') !== -1 || header.indexOf('date of') !== -1) {
        newRow[i] = data.accidentDate || '';
      }
    }

    // Append the filled row to the sheet
    sheet.appendRow(newRow);
    Logger.log('Successfully routed and appended patient "' + data.patientName + '" to "' + sheetName + '" sheet tab.');
  } catch (err) {
    Logger.log('Error adding patient to tab "' + sheetName + '": ' + err.toString());
  }
}

/**
 * Automatically routes patient referrals to the general "new referrals" tab,
 * inserting the patient above the "Seen Patients" header if present to maintain organization.
 *
 * @param {Object} data - Structured patient data
 * @param {string} folderUrl - Patient folder URL
 * @param {string} referralFileUrl - URL of the uploaded PDF referral document (if present)
 * @param {string} referralFileName - Name of the uploaded PDF referral document (if present)
 */
function addPatientToNewReferralsTab(data, folderUrl, referralFileUrl, referralFileName) {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) return;

    var sheet = ss.getSheetByName('new referrals');
    if (!sheet) {
      Logger.log('WARNING: "new referrals" sheet tab not found. Skipping auto-routing.');
      return;
    }

    // Read column headers
    var lastCol = sheet.getLastColumn();
    if (lastCol === 0) {
      Logger.log('WARNING: "new referrals" sheet is empty. Cannot determine column structure.');
      return;
    }

    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];

    // De-duplication check: scan sheet rows for this patient
    var lastRow = sheet.getLastRow();
    var isDuplicate = false;

    if (lastRow > 1) {
      // Read all names in column A
      var names = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
      for (var r = 0; r < names.length; r++) {
        var rowName = String(names[r][0]).trim().toLowerCase();
        if (rowName === data.patientName.toLowerCase().trim()) {
          isDuplicate = true;
          break;
        }
      }
    }

    if (isDuplicate) {
      Logger.log('Patient "' + data.patientName + '" is already listed in "new referrals" tab. Skipping duplicate.');
      return;
    }

    // Construct the new row matching headers
    var newRow = new Array(headers.length);
    for (var i = 0; i < headers.length; i++) {
      newRow[i] = '';
    }

    var currentDateStr = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'M/d/yyyy');

    for (var i = 0; i < headers.length; i++) {
      var header = String(headers[i]).toLowerCase().trim();

      // 1. Name
      if (header.indexOf('name') !== -1) {
        newRow[i] = data.patientName;
      }
      // 2. Referral Date
      else if (header.indexOf('referral date') !== -1 || header.indexOf('date') !== -1) {
        newRow[i] = currentDateStr;
      }
      // 3. Received from
      else if (header.indexOf('received from') !== -1 || header.indexOf('from') !== -1) {
        newRow[i] = data.referringDoctor || '';
      }
      // 4. Type
      else if (header.indexOf('type') !== -1) {
        newRow[i] = data.isWorkInjury ? 'WC' : 'MVA';
      }
      // 5. Insurance
      else if (header.indexOf('insurance') !== -1) {
        newRow[i] = data.insurance || 'Idk'; // Default to 'Idk' if not extracted, matching user's screenshot
      }
      // 6. Referral File Link
      else if (header.indexOf('referral') !== -1) {
        if (referralFileUrl && referralFileName) {
          newRow[i] = '=HYPERLINK("' + referralFileUrl + '", "📄 ' + referralFileName + '")';
        } else {
          newRow[i] = '=HYPERLINK("' + folderUrl + '", "📁 Patient Folder")';
        }
      }
    }

    // Find the row containing "Seen Patients" in column A to insert the new referral before it
    var insertRowIndex = -1;
    if (lastRow > 1) {
      var firstColumnValues = sheet.getRange(1, 1, lastRow, 1).getValues();
      for (var r = 0; r < firstColumnValues.length; r++) {
        var val = String(firstColumnValues[r][0]).trim().toLowerCase();
        if (val.indexOf('seen patients') !== -1) {
          insertRowIndex = r + 1; // 1-indexed row number
          break;
        }
      }
    }

    if (insertRowIndex !== -1) {
      // Insert a row right before "Seen Patients" to keep active referrals grouped at the top
      sheet.insertRowBefore(insertRowIndex);
      sheet.getRange(insertRowIndex, 1, 1, newRow.length).setValues([newRow]);
      Logger.log('Inserted new referral row at row ' + insertRowIndex + ' (above Seen Patients) for: ' + data.patientName);
    } else {
      // If "Seen Patients" is not found, just append to the bottom
      sheet.appendRow(newRow);
      Logger.log('Appended new referral row at the bottom for: ' + data.patientName);
    }
  } catch (err) {
    Logger.log('Error adding patient to "new referrals" tab: ' + err.toString());
  }
}

/**
 * Handles sending welcome or missing document emails to the patient.
 *
 * @param {Object} data - Patient data
 * @param {string} formUrl - Intake form URL
 * @param {Object} config - System configuration
 */
function sendPatientEmails(data, formUrl, config) {
  if (!data.patientEmail) {
    Logger.log('Skipping patient email: No email address extracted.');
    return;
  }

  var hasMissingDocs = (data.missingDocs && data.missingDocs.length > 0);
  var htmlBody = '';
  var subject = '';

  if (hasMissingDocs) {
    subject = 'Action Required: Finish Your Pre-Registration File - Patient Intake Department';
    htmlBody = buildPatientMissingDocsEmail(data.patientName, data.missingDocs);
  } else {
    subject = 'Welcome! Action Required: Complete Your Pre-Visit Forms';
    htmlBody = buildPatientWelcomeEmail(data.patientName, formUrl);
  }

  var plainTextBody = 'Hello ' + data.patientName + ',\n\nPlease open this email in an HTML-compatible client to view your registration details. Your pre-visit intake link is: ' + formUrl;

  dispatchEmail(data.patientEmail, subject, plainTextBody, htmlBody, config);
}

/**
 * Sends a legal-medical coordination email to the attorney if details exist.
 *
 * @param {Object} data - Patient data
 * @param {Object} config - System configuration
 */
function sendAttorneyEmail(data, config) {
  if (!data.attorneyEmail) {
    Logger.log('Skipping attorney coordination: No attorney email address extracted.');
    return;
  }

  var subject = 'Legal-Medical Coordination Case Inquiry - Client: ' + data.patientName;
  var htmlBody = buildAttorneyInquiryEmail(data.attorneyName, data.patientName, data.accidentDate);
  var plainTextBody = 'Dear Counselor,\n\nOur clinic has received a medical referral for your client, ' + data.patientName + '. Please verify the litigation status of this case and if active medical liens or LOP apply.';

  dispatchEmail(data.attorneyEmail, subject, plainTextBody, htmlBody, config);
}

/**
 * Core email sender that switches between automated sending and human-in-the-loop draft queues.
 */
function dispatchEmail(recipient, subject, plainBody, htmlBody, config) {
  var isDraftMode = config.HUMAN_IN_THE_LOOP_MODE;

  try {
    if (isDraftMode) {
      GmailApp.createDraft(recipient, subject, plainBody, {
        htmlBody: htmlBody
      });
      Logger.log('Created GMAIL DRAFT for: ' + recipient + ' (Subject: ' + subject + ')');
    } else {
      GmailApp.sendEmail(recipient, subject, plainBody, {
        htmlBody: htmlBody
      });
      Logger.log('SENT AUTOMATED EMAIL to: ' + recipient + ' (Subject: ' + subject + ')');
    }
  } catch (err) {
    Logger.log('Error dispatching email to ' + recipient + ': ' + err.toString());
  }
}

// ================= GMAIL LABEL HELPER FUNCTIONS =================

function applyLabelToThread(thread, labelName) {
  if (!labelName) return;
  try {
    var label = GmailApp.getUserLabelByName(labelName);
    if (!label) {
      label = GmailApp.createLabel(labelName);
    }
    thread.addLabel(label);
  } catch (e) {
    Logger.log('Error applying label ' + labelName + ': ' + e.toString());
  }
}

function removeLabelFromThread(thread, labelName) {
  if (!labelName) return;
  try {
    var label = GmailApp.getUserLabelByName(labelName);
    if (label) {
      thread.removeLabel(label);
    }
  } catch (e) {
    Logger.log('Error removing label ' + labelName + ': ' + e.toString());
  }
}

// ================= TEST UTILITY / DEBUGGING DRY RUNS =================

/**
 * Mock Dry Run Function
 * Executing this function runs the entire pipeline from folder creation,
 * template replacement, Google Sheet Logging, and Gmail Draft composing
 * using simulated patient data. Use this function in AI Studio / Apps Script editor
 * to test and view the system in action instantly without emails in your inbox!
 */
function runMockDryRun() {
  Logger.log('Starting Mock Dry Run Test...');

  var config = getSystemConfig();
  if (!config.GEMINI_API_KEY) {
    Logger.log('WARNING: Mock dry run using generic setup. Be sure to configure GEMINI_API_KEY for real referral runs.');
  }

  // Simulated structured patient data matching Gemini output
  var mockData = {
    patientName: 'Jane Doe Mock',
    patientDOB: '1990-11-23',
    patientPhone: '(555) 019-2834',
    patientEmail: 'jane.doe.mock.patient@example.com',
    attorneyName: 'Franklin & Associates Law Group',
    attorneyEmail: 'legal.intakes@franklinlawmock.com',
    accidentDate: '2026-04-12',
    referringDoctor: 'Dr. Weinerman / Arcilia',
    isHeadInjury: true,
    isWorkInjury: true,
    insurance: 'Farmers Insurance',
    injuries: [
      'Concussion / Post-Concussive Syndrome',
      'Cervical radiculopathy / Whiplash neck pain',
      'Acute myofascial lower back spasm'
    ],
    clinicalSummary: 'Patient Jane Doe Mock is a 35-year-old female referred by Dr. Weinerman following a high-impact motor vehicle collision on April 12, 2026. She presents with persistent tension headaches, post-concussion dizziness, and severe neck discomfort radiating into her right shoulder. General neurological screening shows normal motor function, but mild cognitive fatigue is reported. She also reports deep musculotendinous lower back tenderness. Urgent diagnostic workups are indicated to verify cervical nerve entrapment.',
    missingDocs: [
      'Copy of Driver\'s License',
      'Auto Insurance Declarations Page',
      'Prior Cervical Spine X-Ray / MRI Reports'
    ]
  };

  Logger.log('Executing Mock Pipeline for: ' + mockData.patientName);

  // 1. Create Patient Folders
  var folders = createPatientDriveFolder(mockData, [], config);
  Logger.log('Mock Drive Folder Generated: ' + folders.folderUrl);

  // 2. Populate Templates
  var docLinks = fillDocumentTemplates(mockData, folders.folderId, config);

  // 3. Generate Form Link
  var prefilledUrl = generatePrefilledFormUrl(mockData, config.PRE_VISIT_FORM_URL);

  // 4. Log in Spreadsheet
  logToSpreadsheetDashboard(mockData, folders.folderUrl, docLinks, prefilledUrl);
  Logger.log('Logged entry in Sheet Dashboard successfully.');

  // 4.25. Automatically route mock patient to "new referrals" tab
  Logger.log('Mock Patient routing to "new referrals" tab...');
  addPatientToNewReferralsTab(mockData, folders.folderUrl, folders.firstFileUrl, folders.firstFileName);

  // 4.5. Automatically route mock patient to specialized tracking tabs if matching criteria are met
  if (mockData.isHeadInjury) {
    Logger.log('Mock Patient has head injury markers. Routing to "Head Injury" tab...');
    addPatientToSpecificTab('Head Injury', mockData, folders.folderUrl);
  }
  if (mockData.isWorkInjury) {
    Logger.log('Mock Patient has work-related injury markers. Routing to "WC Patients" tab...');
    addPatientToSpecificTab('WC Patients', mockData, folders.folderUrl);
  }

  // 5. Create Drafts
  // Force Draft Mode for safety during test
  var testConfig = {};
  Object.keys(config).forEach(function(k) { testConfig[k] = config[k]; });
  testConfig.HUMAN_IN_THE_LOOP_MODE = true;

  Logger.log('Drafting mock emails (review these in your Gmail drafts folder!)...');
  sendPatientEmails(mockData, prefilledUrl, testConfig);
  sendAttorneyEmail(mockData, testConfig);

  Logger.log('Mock Dry Run successfully executed. Review your Google Sheet Active Intake tab, Google Drive folder, and Gmail Drafts!');
}

// =============================================================================
// 9. PHONE INTAKE WEB PORTAL SYNCHRONIZATION ENGINE
// =============================================================================

/**
 * Main Trigger to run Phone Intake Sync manually or schedule on a timer
 */
function runPhoneSyncTrigger() {
  Logger.log('Initiating manual Phone Intake sync trigger...');
  syncPhoneIntakeToWorkspace();
  syncSurgeryCenterStatusesFromDrive();
}

/**
 * Periodically polls the interns' AI Phone Intake website,
 * scrapes the latest patient records with transcripts, and auto-provisions
 * their folders and "Phone Call Intake Notes" documents.
 */
function syncPhoneIntakeToWorkspace() {
  Logger.log('Starting Phone Intake synchronization...');
  var config = getSystemConfig();

  var portalUrl = config.PHONE_INTAKE_URL;
  var username = config.PHONE_INTAKE_USER;
  var password = config.PHONE_INTAKE_PASS;

  if (!portalUrl || !username || !password) {
    Logger.log('ERROR: PHONE_INTAKE credentials not fully configured in settings. Sync stopped.');
    return;
  }

  // Construct Basic Authentication header
  var authHeader = 'Basic ' + Utilities.base64Encode(username + ':' + password);
  var options = {
    method: 'get',
    headers: {
      'Authorization': authHeader,
      'User-Agent': 'Mozilla/5.0 (Google Apps Script Sync Engine)'
    },
    muteHttpExceptions: true
  };

  try {
    Logger.log('Fetching patients index from ' + portalUrl + '/patients...');
    var response = UrlFetchApp.fetch(portalUrl + '/patients', options);
    var responseCode = response.getResponseCode();

    if (responseCode !== 200) {
      Logger.log('ERROR: Portal returned status code ' + responseCode + '. Please verify credentials.');
      return;
    }

    var html = response.getContentText();

    // Extract unique patient profile links (/patients/ID)
    var pidRegex = /\/patients\/(\d+)/gi;
    var matches;
    var pids = [];

    while ((matches = pidRegex.exec(html)) !== null) {
      var pid = matches[1];
      if (pids.indexOf(pid) === -1) {
        pids.push(pid);
      }
    }

    Logger.log('Found ' + pids.length + ' patient profile(s) on AI Phone Intake portal.');

    // Sync top 10 patient profiles at a time to prevent script execution timeout
    var syncLimit = Math.min(pids.length, 10);
    for (var i = 0; i < syncLimit; i++) {
      var pid = pids[i];
      try {
        syncPatientProfile(pid, portalUrl, options, config);
      } catch (err) {
        Logger.log('Error syncing patient profile ID ' + pid + ': ' + err.toString());
      }
    }

    Logger.log('Phone Intake portal synchronization completed.');
    syncSurgeryCenterStatusesFromDrive();
  } catch (err) {
    Logger.log('Error fetching Phone Intake portal: ' + err.toString());
  }
}

/**
 * Fetches an individual patient profile, extracts clinical details, and provisions Drive & Sheets assets.
 */
function syncPatientProfile(pid, baseUrl, options, config) {
  var url = baseUrl + '/patients/' + pid;
  Logger.log('Syncing patient profile #' + pid + '...');

  var response = UrlFetchApp.fetch(url, options);
  if (response.getResponseCode() !== 200) {
    Logger.log('WARNING: Failed to fetch patient #' + pid + ' (Code: ' + response.getResponseCode() + ')');
    return;
  }

  var html = response.getContentText();

  // Extract patient details using robust regex matching
  var nameMatch = html.match(/<h1>\s*([^<]+)\s*<\/h1>/i);
  var patientName = nameMatch ? nameMatch[1].trim() : '';

  if (!patientName || patientName.indexOf('Patient #') !== -1 || patientName.toLowerCase().indexOf('not provided') !== -1) {
    Logger.log('Bypassing profile #' + pid + ': No valid patient name found.');
    return;
  }

  var dobMatch = html.match(/<strong>DOB<\/strong>\s*<\/div>\s*<div[^>]*>\s*([^<]+)\s*<\/div>/i);
  var patientDOB = dobMatch ? dobMatch[1].trim() : '';
  if (patientDOB.toLowerCase().indexOf('not provided') !== -1) patientDOB = '';

  var phoneMatch = html.match(/<strong>Phone<\/strong>\s*<\/div>\s*<div[^>]*>\s*([^<]+)\s*<\/div>/i);
  var patientPhone = phoneMatch ? phoneMatch[1].trim() : '';
  if (patientPhone.toLowerCase().indexOf('not provided') !== -1) patientPhone = '';

  var emailMatch = html.match(/<strong>Email<\/strong>\s*<\/div>\s*<div[^>]*>\s*([^<]+)\s*<\/div>/i);
  var patientEmail = emailMatch ? emailMatch[1].trim() : '';
  if (emailMatch && patientEmail.toLowerCase().indexOf('not provided') !== -1) patientEmail = '';

  var transcriptMatch = html.match(/<div class="transcript"[^>]* id="transcript-text">\s*([\s\S]*?)\s*<\/div>/i);
  var transcript = transcriptMatch ? transcriptMatch[1].trim() : 'No transcript available.';

  // Extract notes summaries
  var summaries = [];
  var descRegex = /<p class="call-desc">([^<]+)<\/p>/gi;
  var descMatch;
  while ((descMatch = descRegex.exec(html)) !== null) {
    summaries.push(descMatch[1].trim());
  }

  Logger.log('Parsed patient: ' + patientName + ' | DOB: ' + patientDOB + ' | Phone: ' + patientPhone);

  // De-duplication check: Check if patient is already logged in Google Sheets dashboard
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var dashboardSheet = ss ? ss.getSheetByName('Active Intake Queue') : null;
  var existingRowIndex = -1;
  var existingFolderUrl = '';

  if (dashboardSheet) {
    var lastRow = dashboardSheet.getLastRow();
    if (lastRow > 1) {
      var values = dashboardSheet.getRange(2, 1, lastRow - 1, 12).getValues();
      for (var i = 0; i < values.length; i++) {
        var rowName = String(values[i][0]).toLowerCase().trim();
        var rowDob = String(values[i][1]).toLowerCase().trim();
        if (rowName === patientName.toLowerCase().trim() && (rowDob === patientDOB.toLowerCase().trim() || !patientDOB)) {
          existingRowIndex = i + 2;
          existingFolderUrl = values[i][11]; // Drive Folder URL column
          break;
        }
      }
    }
  }

  // Standardize clinical attributes for our template generators
  var patientData = {
    patientName: patientName,
    patientDOB: patientDOB,
    patientPhone: patientPhone,
    patientEmail: patientEmail,
    attorneyName: '',
    attorneyEmail: '',
    accidentDate: '',
    referringDoctor: 'AI Phone Intake',
    isHeadInjury: (transcript.toLowerCase().indexOf('head') !== -1 || transcript.toLowerCase().indexOf('concussion') !== -1 || transcript.toLowerCase().indexOf('headache') !== -1),
    isWorkInjury: (transcript.toLowerCase().indexOf('work') !== -1 || transcript.toLowerCase().indexOf('wc') !== -1 || transcript.toLowerCase().indexOf('job') !== -1),
    insurance: 'Farmers',
    injuries: ['AI Call Intake Complaints'],
    clinicalSummary: summaries.join(' ') || 'Patient completed AI Phone Intake call. See detailed transcript document in records folder.',
    missingDocs: ['Copy of Driver\'s License', 'Copy of Health Insurance Card']
  };

  var folderMetadata;

  if (existingRowIndex !== -1 && existingFolderUrl) {
    Logger.log('Patient ' + patientName + ' already exists in Dashboard. Verifying if Phone Notes Google Doc exists...');

    // Extract Folder ID from existing URL
    var folderIdMatch = existingFolderUrl.match(/folders\/([a-zA-Z0-9_-]+)/);
    var folderId = folderIdMatch ? folderIdMatch[1] : '';

    if (folderId) {
      // Sync/Create Phone Call Intake Notes inside existing folder
      createPhoneNotesDoc(patientData, folderId, transcript, summaries);
    }
  } else {
    Logger.log('Patient ' + patientName + ' is new! Initiating folder creation and pipeline...');

    // 1. Create Patient Folders in Drive
    folderMetadata = createPatientDriveFolder(patientData, [], config);

    // 2. Create the beautiful "Phone Call Intake Notes" Google Doc inside records subfolder
    var phoneNotesUrl = createPhoneNotesDoc(patientData, folderMetadata.folderId, transcript, summaries);

    // 3. Fill out standard case summaries & prefilled intake forms
    var docLinks = fillDocumentTemplates(patientData, folderMetadata.folderId, config);
    var prefilledUrl = generatePrefilledFormUrl(patientData, config.PRE_VISIT_FORM_URL);

    // 4. Log to spreadsheets dashboard
    logToSpreadsheetDashboard(patientData, folderMetadata.folderUrl, docLinks, prefilledUrl);

    // 5. Automatically route to "new referrals" and specialized sheets above seen lines
    addPatientToNewReferralsTab(patientData, folderMetadata.folderUrl, phoneNotesUrl, '📞 Phone Intake Notes');

    if (patientData.isHeadInjury) {
      addPatientToSpecificTab('Head Injury', patientData, folderMetadata.folderUrl);
    }
    if (patientData.isWorkInjury) {
      addPatientToSpecificTab('WC Patients', patientData, folderMetadata.folderUrl);
    }

    // 6. human-in-the-loop: Compose welcome drafts
    sendPatientEmails(patientData, prefilledUrl, config);
  }
}

/**
 * Creates a beautifully styled "Phone Call Intake Notes" Google Doc inside the patient's records subfolder.
 */
function createPhoneNotesDoc(data, folderId, transcript, summaries) {
  try {
    var parentFolder = DriveApp.getFolderById(folderId);
    var recordsFolder;
    var recordsSubfolders = parentFolder.getFoldersByName('Referral & Medical Records');

    if (recordsSubfolders.hasNext()) {
      recordsFolder = recordsSubfolders.next();
    } else {
      recordsFolder = parentFolder.createFolder('Referral & Medical Records');
    }

    var docName = data.patientName + ' - Phone Intake Notes';

    // Check for duplicates to prevent piling files
    var existingFiles = recordsFolder.getFilesByName(docName);
    if (existingFiles.hasNext()) {
      var file = existingFiles.next();
      Logger.log('Phone Intake Notes document already exists for: ' + data.patientName);
      return file.getUrl();
    }

    // Create new document
    var doc = DocumentApp.create(docName);
    var docFile = DriveApp.getFileById(doc.getId());
    docFile.moveTo(recordsFolder);

    var body = doc.getBody();
    var currentDate = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM/dd/yyyy HH:mm');

    // Title
    var title = body.appendParagraph("PHONE CALL INTAKE RECORD");
    title.setHeading(DocumentApp.ParagraphHeading.TITLE).setAlignment(DocumentApp.HorizontalAlignment.CENTER);
    title.editAsText().setFontFamily("Calibri").setBold(true).setForegroundColor("#1F6F78");

    // Sync Metadata
    var metaPara = body.appendParagraph("Synced Date: " + currentDate + " | Source: AI Phone Agent");
    metaPara.setAlignment(DocumentApp.HorizontalAlignment.RIGHT);
    metaPara.editAsText().setFontSize(10).setItalic(true);

    // Demographics section
    var section1 = body.appendParagraph("Patient Call Profile & Demographics");
    section1.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    section1.editAsText().setForegroundColor("#1F6F78").setBold(true);

    body.appendParagraph("Patient Full Name: " + data.patientName);
    body.appendParagraph("Date of Birth: " + (data.patientDOB || 'Not provided'));
    body.appendParagraph("Primary Contact Phone: " + (data.patientPhone || 'Not provided'));
    body.appendParagraph("Email Address: " + (data.patientEmail || 'Not provided'));
    body.appendParagraph("Injury Category: " + (data.isWorkInjury ? "Workers' Compensation" : "Personal Injury / MVA"));

    body.appendHorizontalRule();

    // Call Transcription
    var section2 = body.appendParagraph("Voice Recording Transcription");
    section2.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    section2.editAsText().setForegroundColor("#1F6F78").setBold(true);

    var trPara = body.appendParagraph(transcript || "No phone voice recording transcript found.");
    trPara.editAsText().setFontSize(10.5).setFontFamily("Consolas").setForegroundColor("#2d3748");
    trPara.setLineSpacing(1.15);

    body.appendHorizontalRule();

    // AI Summaries
    var section3 = body.appendParagraph("Intake Action Summaries");
    section3.setHeading(DocumentApp.ParagraphHeading.HEADING2);
    section3.editAsText().setForegroundColor("#1F6F78").setBold(true);

    if (summaries && summaries.length > 0) {
      for (var i = 0; i < summaries.length; i++) {
        var summaryPara = body.appendParagraph("• " + summaries[i]);
        summaryPara.editAsText().setFontSize(11);
      }
    } else {
      body.appendParagraph("No summarization notes logged during the call.");
    }

    doc.saveAndClose();
    Logger.log('Successfully created Phone Intake Notes Google Doc for ' + data.patientName + ': ' + docFile.getUrl());
    return docFile.getUrl();
  } catch (err) {
    Logger.log('Error creating Phone Notes Doc: ' + err.toString());
    return '';
  }
}

/**
 * Synchronizes Surgery Center Statuses from Google Drive physical folder tags
 * back into the Google Spreadsheet dashboard tabs.
 */
function syncSurgeryCenterStatusesFromDrive() {
  Logger.log('Starting Surgery Center Status synchronization from Google Drive folders...');
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) return;

  // Sync across both "new referrals" and "Active Intake Queue" sheets
  var sheetNames = ['new referrals', 'Active Intake Queue'];

  for (var s = 0; s < sheetNames.length; s++) {
    var sheet = ss.getSheetByName(sheetNames[s]);
    if (!sheet) continue;

    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow <= 1 || lastCol === 0) continue;

    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];

    // Find column indexes (1-based)
    var folderColIdx = -1;
    var statusColIdx = -1;
    var nameColIdx = -1;

    for (var i = 0; i < headers.length; i++) {
      var header = String(headers[i]).toLowerCase().trim();
      if (header.indexOf('folder') !== -1 || header.indexOf('referral') !== -1 || header.indexOf('drive folder link') !== -1) {
        folderColIdx = i + 1;
      }
      if (header.indexOf('surgery center status') !== -1) {
        statusColIdx = i + 1;
      }
      if (header.indexOf('name') !== -1) {
        nameColIdx = i + 1;
      }
    }

    if (folderColIdx === -1 || statusColIdx === -1) {
      Logger.log('WARNING: Sheet "' + sheetNames[s] + '" is missing Folder/Referral column or Surgery Center Status column. Skipping.');
      continue;
    }

    Logger.log('Syncing sheet: ' + sheetNames[s] + ' (Folder Col: ' + folderColIdx + ', Status Col: ' + statusColIdx + ')');

    var range = sheet.getRange(2, 1, lastRow - 1, lastCol);
    var formulas = range.getFormulas();
    var values = range.getValues();

    for (var r = 0; r < values.length; r++) {
      var folderVal = values[r][folderColIdx - 1];
      var folderFormula = formulas[r][folderColIdx - 1];
      var patientName = nameColIdx !== -1 ? values[r][nameColIdx - 1] : 'Row ' + (r + 2);

      var url = '';
      if (folderFormula) {
        var match = folderFormula.match(/HYPERLINK\("([^"]+)"/i);
        if (match) {
          url = match[1];
        }
      } else if (folderVal && String(folderVal).indexOf('http') !== -1) {
        url = String(folderVal);
      }

      if (!url) continue;

      var folderIdMatch = url.match(/folders\/([a-zA-Z0-9_-]+)/);
      var folderId = folderIdMatch ? folderIdMatch[1] : '';

      if (!folderId) continue;

      try {
        var folder = DriveApp.getFolderById(folderId);
        var files = folder.getFiles();
        var driveStatus = '';

        while (files.hasNext()) {
          var file = files.next();
          var fileName = file.getName();
          if (fileName.startsWith('SURGERY_STATUS_') && fileName.endsWith('.txt')) {
            var statusStr = fileName.substring('SURGERY_STATUS_'.length, fileName.length - '.txt'.length).toLowerCase();
            driveStatus = statusStr.charAt(0).toUpperCase() + statusStr.slice(1);
            break;
          }
        }

        if (driveStatus) {
          var currentStatus = String(values[r][statusColIdx - 1]).trim();
          if (driveStatus.toLowerCase() !== currentStatus.toLowerCase()) {
            Logger.log('UPDATED: Patient "' + patientName + '" status out-of-sync. Google Drive: ' + driveStatus + ', Spreadsheet: ' + currentStatus + '. Updating row ' + (r + 2) + '...');
            sheet.getRange(r + 2, statusColIdx).setValue(driveStatus);
          }
        }
      } catch (err) {
        Logger.log('Error scanning folder for row ' + (r + 2) + ' (' + patientName + '): ' + err.toString());
      }
    }
  }
  Logger.log('Surgery Center Status synchronization completed.');
}

/**
 * Webhook endpoint for direct integration with external services (like the Python API server).
 * Receives JSON payloads to update data in real-time.
 */
function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    if (data.action === "updateSurgeryStatus") {
      var result = updateSurgeryStatusDirectly(data.patientName, data.status);
      return ContentService.createTextOutput(JSON.stringify({success: true, rowsUpdated: result}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    else if (data.action === "updatePatient") {
      var result = updatePatientDirectly(data.patientName, data.patientDOB, data.fields);
      return ContentService.createTextOutput(JSON.stringify({success: true, message: "Patient updated in sheets", rowsUpdated: result}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    else if (data.action === "updatePatientTask") {
      var result = updatePatientTaskDirectly(data.patientName, data.taskKey, data.status, data.assignee, data.notes);
      return ContentService.createTextOutput(JSON.stringify({success: true, message: "Patient task updated in sheets", rowsUpdated: result}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    else if (data.action === "appendPatientNote") {
      var result = appendPatientNoteDirectly(data.patientName, data.patientDOB, data.note, data.author);
      return ContentService.createTextOutput(JSON.stringify({success: true, message: "Note appended to Google Doc", docUrl: result}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    else if (data.action === "testConnection") {
      return ContentService.createTextOutput(JSON.stringify({success: true, message: "Apps Script connection verified!"}))
        .setMimeType(ContentService.MimeType.JSON);
    }
    return ContentService.createTextOutput(JSON.stringify({error: "Unknown action"}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch(err) {
    return ContentService.createTextOutput(JSON.stringify({error: err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

/**
 * Directly updates the Google Sheet row for a given patient without scanning Google Drive.
 */
function updateSurgeryStatusDirectly(patientName, newStatus) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetNames = ['new referrals', 'Active Intake Queue'];
  var updated = 0;

  for (var s = 0; s < sheetNames.length; s++) {
    var sheet = ss.getSheetByName(sheetNames[s]);
    if (!sheet) continue;

    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow <= 1 || lastCol === 0) continue;

    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    var statusColIdx = -1;
    var nameColIdx = -1;

    for (var i = 0; i < headers.length; i++) {
      var header = String(headers[i]).toLowerCase().trim();
      if (header.indexOf('surgery center status') !== -1 || header.indexOf('intake status') !== -1) {
        statusColIdx = i + 1;
      }
      if (header.indexOf('name') !== -1) {
        nameColIdx = i + 1;
      }
    }

    if (statusColIdx === -1 || nameColIdx === -1) continue;

    var range = sheet.getRange(2, 1, lastRow - 1, lastCol);
    var values = range.getValues();

    for (var r = 0; r < values.length; r++) {
      var rowName = values[r][nameColIdx - 1];
      if (rowName && String(rowName).toLowerCase().trim() === patientName.toLowerCase().trim()) {
        sheet.getRange(r + 2, statusColIdx).setValue(newStatus);
        updated++;
      }
    }
  }
  return updated;
}

/**
 * Updates full patient details (demographics, insurance, attorney, status) in Google Sheets.
 */
function updatePatientDirectly(patientName, patientDOB, fields) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheetNames = ['Active Intake Queue', 'new referrals'];
  var updated = 0;

  for (var s = 0; s < sheetNames.length; s++) {
    var sheet = ss.getSheetByName(sheetNames[s]);
    if (!sheet) continue;

    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow <= 1 || lastCol === 0) continue;

    var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
    var colMap = {};
    for (var i = 0; i < headers.length; i++) {
      colMap[String(headers[i]).toLowerCase().trim()] = i + 1;
    }

    var nameColIdx = colMap['patient name'] || colMap['name'];
    var dobColIdx = colMap['dob'];

    if (!nameColIdx) continue;

    var range = sheet.getRange(2, 1, lastRow - 1, lastCol);
    var values = range.getValues();

    for (var r = 0; r < values.length; r++) {
      var rowName = values[r][nameColIdx - 1];
      var rowDob = dobColIdx ? values[r][dobColIdx - 1] : '';

      var matches = false;
      if (rowName && String(rowName).toLowerCase().trim() === patientName.toLowerCase().trim()) {
        if (!patientDOB || !rowDob || String(rowDob).toLowerCase().trim().indexOf(String(patientDOB).toLowerCase().trim()) !== -1) {
          matches = true;
        }
      }

      if (matches) {
        var rowNum = r + 2;

        // Save new values if present and sheet has matching column
        if (fields.patientName && colMap['patient name']) sheet.getRange(rowNum, colMap['patient name']).setValue(fields.patientName);
        if (fields.patientName && colMap['name']) sheet.getRange(rowNum, colMap['name']).setValue(fields.patientName);
        if (fields.patientDOB && colMap['dob']) sheet.getRange(rowNum, colMap['dob']).setValue(fields.patientDOB);
        if (fields.patientPhone && colMap['phone']) sheet.getRange(rowNum, colMap['phone']).setValue(fields.patientPhone);
        if (fields.patientEmail && colMap['email']) sheet.getRange(rowNum, colMap['email']).setValue(fields.patientEmail);
        if (fields.attorneyName && colMap['attorney name']) sheet.getRange(rowNum, colMap['attorney name']).setValue(fields.attorneyName);
        if (fields.attorneyEmail && colMap['attorney email']) sheet.getRange(rowNum, colMap['attorney email']).setValue(fields.attorneyEmail);
        if (fields.isHeadInjury !== undefined && colMap['is head injury?']) sheet.getRange(rowNum, colMap['is head injury?']).setValue(fields.isHeadInjury ? 'Yes' : 'No');
        if (fields.isWorkInjury !== undefined && colMap['is work injury?']) sheet.getRange(rowNum, colMap['is work injury?']).setValue(fields.isWorkInjury ? 'Yes' : 'No');
        if (fields.insurance && colMap['insurance']) sheet.getRange(rowNum, colMap['insurance']).setValue(fields.insurance);

        var statusCol = colMap['surgery center status'] || colMap['intake status'];
        if (statusCol && fields.surgeryStatus) {
          sheet.getRange(rowNum, statusCol).setValue(fields.surgeryStatus);
        }

        if (colMap['last updated']) {
          sheet.getRange(rowNum, colMap['last updated']).setValue(new Date());
        }

        updated++;
      }
    }
  }
  return updated;
}

/**
 * Continuous patient clinical notes sync. Appends chronological notes to a single Patient Google Doc.
 */
function appendPatientNoteDirectly(patientName, patientDOB, note, author) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('Active Intake Queue');
  var folderId = '';

  if (sheet) {
    var lastRow = sheet.getLastRow();
    var lastCol = sheet.getLastColumn();
    if (lastRow > 1) {
      var headers = sheet.getRange(1, 1, 1, lastCol).getValues()[0];
      var nameColIdx = -1;
      var dobColIdx = -1;
      var folderColIdx = -1;

      for (var i = 0; i < headers.length; i++) {
        var h = String(headers[i]).toLowerCase().trim();
        if (h.indexOf('name') !== -1) nameColIdx = i + 1;
        if (h.indexOf('dob') !== -1) dobColIdx = i + 1;
        if (h.indexOf('folder') !== -1 || h.indexOf('drive folder link') !== -1) folderColIdx = i + 1;
      }

      if (nameColIdx !== -1 && folderColIdx !== -1) {
        var range = sheet.getRange(2, 1, lastRow - 1, lastCol);
        var values = range.getValues();
        for (var r = 0; r < values.length; r++) {
          var rowName = values[r][nameColIdx - 1];
          var rowDob = dobColIdx !== -1 ? values[r][dobColIdx - 1] : '';

          var matches = false;
          if (rowName && String(rowName).toLowerCase().trim() === patientName.toLowerCase().trim()) {
            if (!patientDOB || !rowDob || String(rowDob).toLowerCase().trim().indexOf(String(patientDOB).toLowerCase().trim()) !== -1) {
              matches = true;
            }
          }

          if (matches) {
            var folderUrl = values[r][folderColIdx - 1];
            if (folderUrl) {
              var folderIdMatch = folderUrl.match(/folders\/([a-zA-Z0-9_-]+)/);
              if (folderIdMatch) {
                folderId = folderIdMatch[1];
                break;
              }
            }
          }
        }
      }
    }
  }

  // Fallback: If we couldn't find the folder ID from the sheet, search the Patient Intakes root folder in Google Drive
  if (!folderId) {
    var config = getSystemConfig();
    var rootFolderId = config.ROOT_DRIVE_FOLDER_ID;
    var rootFolder;
    if (rootFolderId) {
      rootFolder = DriveApp.getFolderById(rootFolderId);
    } else {
      var folders = DriveApp.getFoldersByName('Patient Intakes');
      if (folders.hasNext()) rootFolder = folders.next();
    }

    if (rootFolder) {
      var nameParts = patientName.split(' ');
      var folderName = '';
      if (nameParts.length > 1) {
        var lastName = nameParts[nameParts.length - 1];
        var firstNames = nameParts.slice(0, nameParts.length - 1).join(' ');
        folderName = lastName + ', ' + firstNames;
      } else {
        folderName = patientName;
      }
      if (patientDOB) {
        folderName += ' - DOB ' + patientDOB;
      }

      var folders = rootFolder.getFoldersByName(folderName);
      if (folders.hasNext()) {
        folderId = folders.next().getId();
      }
    }
  }

  if (!folderId) {
    throw new Error("Could not locate Google Drive folder for patient: " + patientName);
  }

  var parentFolder = DriveApp.getFolderById(folderId);
  var recordsFolder;
  var recordsSubfolders = parentFolder.getFoldersByName('Referral & Medical Records');

  if (recordsSubfolders.hasNext()) {
    recordsFolder = recordsSubfolders.next();
  } else {
    recordsFolder = parentFolder.createFolder('Referral & Medical Records');
  }

  var docName = patientName + ' - Clinical Notes';
  var existingFiles = recordsFolder.getFilesByName(docName);
  var doc;

  if (existingFiles.hasNext()) {
    var file = existingFiles.next();
    doc = DocumentApp.openById(file.getId());
    Logger.log('Found existing clinical notes Google Doc: ' + file.getName());
  } else {
    doc = DocumentApp.create(docName);
    var docFile = DriveApp.getFileById(doc.getId());
    docFile.moveTo(recordsFolder);

    var body = doc.getBody();
    var title = body.appendParagraph("CLINICAL PATIENT OPERATIONS NOTES");
    title.setHeading(DocumentApp.ParagraphHeading.TITLE).setAlignment(DocumentApp.HorizontalAlignment.CENTER);
    title.editAsText().setFontFamily("Calibri").setBold(true).setForegroundColor("#1F6F78");

    var desc = body.appendParagraph("Continuous running notes for client: " + patientName + "\nRecord initialized on " + Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM/dd/yyyy'));
    desc.editAsText().setFontSize(11).setItalic(true);
    body.appendHorizontalRule();

    doc.saveAndClose();
    Logger.log('Created new clinical notes Google Doc: ' + docFile.getName());
  }

  var body = doc.getBody();
  var currentDate = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'MM/dd/yyyy HH:mm:ss');

  var noteHeader = body.appendParagraph("[" + currentDate + "] " + (author || 'System User') + ":");
  noteHeader.editAsText().setBold(true).setFontSize(11).setForegroundColor("#2D3748");

  var noteText = body.appendParagraph(note);
  noteText.editAsText().setFontSize(11).setFontFamily("Calibri").setItalic(false);
  noteText.setLineSpacing(1.15);

  body.appendParagraph(""); // Spacer
  doc.saveAndClose();
  return DriveApp.getFileById(doc.getId()).getUrl();
}

/**
 * Directly updates a task row on the central Google Sheet spreadsheet inside the "TeamTasks" tab.
 */
function updatePatientTaskDirectly(patientName, taskKey, status, assignee, notes) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) return 0;

  var sheet = ss.getSheetByName('TeamTasks');
  if (!sheet) {
    sheet = ss.insertSheet('TeamTasks');
    var headers = ['Patient Name', 'Task Key', 'Assignee', 'Status', 'Notes', 'Last Updated'];
    sheet.getRange('A1:F1').setValues([headers]);
    sheet.getRange('A1:F1').setFontWeight('bold').setBackground('#E1F5FE').setFontColor('#01579B');
    sheet.setFrozenRows(1);
  }

  var lastRow = sheet.getLastRow();
  var values = lastRow > 1 ? sheet.getRange(2, 1, lastRow - 1, 4).getValues() : [];
  var existingRowIndex = -1;

  for (var i = 0; i < values.length; i++) {
    var rowName = String(values[i][0]).toLowerCase().trim();
    var rowKey = String(values[i][1]).toLowerCase().trim();

    if (rowName === patientName.toLowerCase().trim() && rowKey === taskKey.toLowerCase().trim()) {
      existingRowIndex = i + 2; // Conversion to 1-indexed row number
      break;
    }
  }

  var currentDate = new Date();
  var rowData = [
    patientName,
    taskKey,
    assignee || 'Unassigned',
    status || 'Pending',
    notes || '',
    currentDate
  ];

  if (existingRowIndex !== -1) {
    sheet.getRange(existingRowIndex, 1, 1, rowData.length).setValues([rowData]);
  } else {
    sheet.appendRow(rowData);
  }

  sheet.autoResizeColumns(1, 6);
  return 1;
}

/**
 * Config.js - Dynamic Configuration Management for Patient Intake Workflow
 * 
 * This module manages all system settings. To prevent hardcoding and simplify maintenance,
 * settings are dynamically loaded from a "Settings" tab in the active Google Spreadsheet.
 * If the spreadsheet or the tab is not yet set up, it gracefully falls back to secure default values.
 */

// Global Configuration Object with default values
var CONFIG_DEFAULTS = {
  // Senders whose emails will be scanned for patient referrals
  SENDER_EMAILS: [
    'mariah@transplexpt.com',                  // Dr. Eshleman / Mariah
    'amiranda@weinermainpainandwellness.com',  // Dr. Weinerman / Arcilia
    'l.olmeda@princetonmmi.com'                // Princeton Brain & Spine / Leslie
  ],
  
  // Gmail labels for state tracking (Sleek Referral Vibe)
  LABEL_NEW: 'Referral/New',
  LABEL_PROCESSED: 'Referral/Processed',
  LABEL_ERROR: 'Referral/Error',
  LABEL_IGNORED: 'Referral/Ignored',
  
  // Gemini API Configuration
  GEMINI_API_KEY: '', // To be filled in the Google Sheet settings
  GEMINI_MODEL: 'gemini-2.0-flash', // Fastest and highly accurate for structured JSON extraction
  
  // Google Drive & Docs Template IDs
  ROOT_DRIVE_FOLDER_ID: '', // Parent folder where patient directories are located
  PATIENT_INTAKE_TEMPLATE_ID: '', // Template Google Doc ID for "Patient Intake Form"
  FEE_TEMPLATE_ID: '', // Template Google Doc ID for "Fee Template"
  WORKERS_COMP_TEMPLATE_ID: '', // Template Google Doc ID for "Workers Comp Form" (only for work injuries)
  HEAD_INJURY_TEMPLATE_ID: '', // Template Google Doc ID for "Head Injury Report" (only for head/concussion injuries)
  
  // Pre-visit Intake Form URL
  PRE_VISIT_FORM_URL: 'https://docs.google.com/forms/d/e/.../viewform',
  
  // Human-in-the-Loop Mode
  // If true, creates Gmail Drafts for review instead of sending emails immediately.
  HUMAN_IN_THE_LOOP_MODE: true,
  
  // Date boundary to prevent scanning old historical emails
  START_DATE: '2026-05-29',
  
  // Phone Intake Portal Configuration
  PHONE_INTAKE_URL: 'https://ai-phone-intake-aimedicalcoach.azurewebsites.net',
  PHONE_INTAKE_USER: 'officeadmin',
  PHONE_INTAKE_PASS: '',

  // Pre-filter toggles (cheap, no AI call needed)
  SKIP_BULK_MAIL: true,       // Skip bulk/marketing blasts (List-Unsubscribe header). Set false to force AI review.
  SKIP_VENDOR_INVOICES: true  // Skip vendor invoices/billing notices without an AI call. Set false to force AI review of every email.
};

/**
 * Retrieves the active configuration. It attempts to read key-value pairs from a "Settings"
 * sheet tab in the active Google Spreadsheet. If not found or empty, it falls back to defaults.
 * 
 * @return {Object} Combined configuration object
 */
function getSystemConfig() {
  var config = {};
  
  // Clone defaults into our active configuration
  Object.keys(CONFIG_DEFAULTS).forEach(function(key) {
    config[key] = CONFIG_DEFAULTS[key];
  });
  
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) {
      Logger.log('WARNING: No active spreadsheet found. Using default script configurations.');
      return config;
    }
    
    var settingsSheet = ss.getSheetByName('Settings');
    if (!settingsSheet) {
      Logger.log('WARNING: "Settings" sheet tab not found. Using default configurations. Please create a tab named "Settings" with Key and Value columns.');
      return config;
    }
    
    // Read cells in Columns A & B (Key, Value) starting from row 2 (skipping header)
    var lastRow = settingsSheet.getLastRow();
    if (lastRow < 2) {
      Logger.log('WARNING: "Settings" sheet tab is empty. Using default configurations.');
      return config;
    }
    
    var range = settingsSheet.getRange(2, 1, lastRow - 1, 2);
    var values = range.getValues();
    
    for (var i = 0; i < values.length; i++) {
      var key = String(values[i][0]).trim();
      var rawVal = values[i][1];
      
      if (!key) continue;
      
      // Parse values appropriately
      if (typeof rawVal === 'string') {
        var trimmedVal = rawVal.trim();
        
        // Parse Booleans
        if (trimmedVal.toLowerCase() === 'true') {
          config[key] = true;
        } else if (trimmedVal.toLowerCase() === 'false') {
          config[key] = false;
        } else if (key === 'SENDER_EMAILS') {
          // Parse comma-separated emails into an array
          config[key] = trimmedVal.split(',').map(function(email) {
            return email.trim().toLowerCase();
          }).filter(Boolean);
        } else {
          config[key] = trimmedVal;
        }
      } else {
        // Numbers, dates, etc., are preserved
        config[key] = rawVal;
      }
    }
    
    Logger.log('Successfully loaded system configuration from "Settings" sheet.');
  } catch (err) {
    Logger.log('Error reading configuration sheet: ' + err.toString() + '. Falling back to defaults.');
  }
  
  return config;
}

/**
 * Utility function to initialize/create the "Settings" sheet tab if it does not exist,
 * pre-populating it with standard keys and helper instructions.
 */
function initializeSettingsSheet() {
  try {
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) {
      throw new Error('SpreadsheetApp.getActiveSpreadsheet() returned null. Open this script from a Google Sheet.');
    }
    
    var settingsSheet = ss.getSheetByName('Settings');
    if (settingsSheet) {
      Logger.log('Settings sheet already exists.');
      return;
    }
    
    settingsSheet = ss.insertSheet('Settings');
    
    // Set headers and styles
    settingsSheet.getRange('A1:C1').setValues([['Setting Key', 'Value', 'Description & Examples']]);
    settingsSheet.getRange('A1:C1').setFontWeight('bold').setBackground('#E0F2F1').setFontColor('#004D40');
    
    var settingsData = [
      ['GEMINI_API_KEY', '', 'Your Google AI Studio Gemini API Key (e.g. AIzaSy...)'],
      ['ROOT_DRIVE_FOLDER_ID', '', 'Google Drive Folder ID where patient folders are located'],
      ['PATIENT_INTAKE_TEMPLATE_ID', '', 'Google Doc Template ID for "Patient Intake Form"'],
      ['FEE_TEMPLATE_ID', '', 'Google Doc Template ID for "Fee Template"'],
      ['WORKERS_COMP_TEMPLATE_ID', '', 'Google Doc Template ID for "Workers Comp Form" (only for work injuries)'],
      ['HEAD_INJURY_TEMPLATE_ID', '', 'Google Doc Template ID for "Head Injury Report" (only for head/concussion injuries)'],
      ['PRE_VISIT_FORM_URL', 'https://docs.google.com/forms/d/e/.../viewform', 'URL of your patient intake pre-visit Google Form'],
      ['SENDER_EMAILS', 'mariah@transplexpt.com, amiranda@weinermainpainandwellness.com, l.olmeda@princetonmmi.com', 'Comma-separated email addresses of referring doctors/coordinators (unused by AI classifier)'],
      ['HUMAN_IN_THE_LOOP_MODE', 'true', 'Set to true to create Gmail drafts for review, or false to auto-send'],
      ['GEMINI_MODEL', 'gemini-2.0-flash', 'Gemini Model to use (default: gemini-2.0-flash)'],
      ['LABEL_NEW', 'Referral/New', 'Gmail label applied to newly detected referrals during processing'],
      ['LABEL_PROCESSED', 'Referral/Processed', 'Gmail label applied after successful processing'],
      ['LABEL_ERROR', 'Referral/Error', 'Gmail label applied when processing encounters a critical failure'],
      ['LABEL_IGNORED', 'Referral/Ignored', 'Gmail label applied to emails classified as not patient referrals'],
      ['START_DATE', '2026-05-29', 'Only process referrals received on or after this date (YYYY-MM-DD format to exclude old history)'],
      ['PHONE_INTAKE_URL', 'https://ai-phone-intake-aimedicalcoach.azurewebsites.net', 'The root URL of your AI Phone Intake web portal'],
      ['PHONE_INTAKE_USER', 'officeadmin', 'Basic Auth username for the AI Phone Intake web portal'],
      ['PHONE_INTAKE_PASS', '', 'Basic Auth password for the AI Phone Intake web portal. Fill this in the private Settings sheet only.'],
      ['SKIP_BULK_MAIL', 'true', 'Skip bulk/marketing blasts (List-Unsubscribe header) without an AI call. Set false to force AI review of every email.'],
      ['SKIP_VENDOR_INVOICES', 'true', 'Skip vendor invoices/billing notices without an AI call. Set false to force AI review of every email.']
    ];
    
    settingsSheet.getRange(2, 1, settingsData.length, 3).setValues(settingsData);
    settingsSheet.autoResizeColumns(1, 3);
    Logger.log('Created and initialized "Settings" sheet tab.');
  } catch (err) {
    Logger.log('Error initializing settings sheet: ' + err.toString());
  }
}

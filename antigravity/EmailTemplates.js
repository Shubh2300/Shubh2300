/**
 * EmailTemplates.js - Premium Responsive HTML Email Templates
 * 
 * Provides beautiful, highly styled, and clinical-focused HTML email bodies for
 * patients and attorneys. These designs use curated HSL color schemes (Slate,
 * Teal, and off-white), high-quality typography (Inter/system-sans), subtle shadows,
 * rounded borders, and clear Calls-To-Action (CTAs).
 */

/**
 * Builds a beautiful patient welcome email with a Call-To-Action button leading
 * to their pre-filled pre-visit intake form.
 * 
 * @param {string} patientName - First name or full name of patient
 * @param {string} prefilledFormUrl - Pre-filled Google Form URL
 * @return {string} HTML email string
 */
function buildPatientWelcomeEmail(patientName, prefilledFormUrl) {
  var welcomeText = "We are pleased to welcome you to our practice! Your referring doctor recently sent over your referral, and our clinical team is actively preparing your digital chart for your upcoming visit.";
  
  return (
    '<!DOCTYPE html>' +
    '<html>' +
    '<head>' +
    '  <meta charset="utf-8">' +
    '  <meta name="viewport" content="width=device-width, initial-scale=1.0">' +
    '  <title>Welcome to Our Practice</title>' +
    '  <style>' +
    '    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f7f8; margin: 0; padding: 0; -webkit-font-smoothing: antialiased; }' +
    '    .wrapper { width: 100%; background-color: #f5f7f8; padding: 40px 20px; box-sizing: border-box; }' +
    '    .container { max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05); overflow: hidden; border: 1px solid #eef1f2; }' +
    '    .header { background: linear-gradient(135deg, #00796b, #004d40); padding: 35px 40px; text-align: left; }' +
    '    .header h1 { color: #ffffff; margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }' +
    '    .header p { color: #b2dfdb; margin: 5px 0 0 0; font-size: 14px; }' +
    '    .content { padding: 40px; color: #2d3748; line-height: 1.6; }' +
    '    .content h2 { font-size: 20px; font-weight: 600; color: #004d40; margin-top: 0; margin-bottom: 16px; }' +
    '    .content p { font-size: 15px; color: #4a5568; margin-bottom: 24px; }' +
    '    .card { background-color: #f0f7f6; border-left: 4px solid #00796b; padding: 20px; border-radius: 0 8px 8px 0; margin-bottom: 30px; }' +
    '    .card-title { font-weight: bold; color: #004d40; font-size: 14px; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }' +
    '    .card-text { font-size: 14px; color: #334e4a; margin: 0; }' +
    '    .cta-area { text-align: center; margin: 35px 0; }' +
    '    .btn { display: inline-block; background-color: #00796b; color: #ffffff !important; font-weight: 600; font-size: 15px; padding: 14px 30px; text-decoration: none; border-radius: 8px; box-shadow: 0 4px 6px rgba(0, 121, 107, 0.2); transition: background-color 0.2s; }' +
    '    .footer { background-color: #f8fafc; padding: 30px 40px; text-align: center; border-top: 1px solid #edf2f7; }' +
    '    .footer p { margin: 0; font-size: 13px; color: #718096; line-height: 1.5; }' +
    '    .footer a { color: #00796b; text-decoration: none; font-weight: 500; }' +
    '  </style>' +
    '</head>' +
    '<body>' +
    '  <div class="wrapper">' +
    '    <div class="container">' +
    '      <div class="header">' +
    '        <h1>Welcome to Our Practice</h1>' +
    '        <p>Your Health & Wellness Journey Begins Here</p>' +
    '      </div>' +
    '      <div class="content">' +
    '        <h2>Hello ' + patientName + ',</h2>' +
    '        <p>' + welcomeText + '</p>' +
    '        ' +
    '        <div class="card">' +
    '          <div class="card-title">Pre-Registration Required</div>' +
    '          <p class="card-text">To ensure a seamless check-in experience and minimize your wait time, please complete your pre-visit clinical intake form prior to your scheduled appointment.</p>' +
    '        </div>' +
    '        ' +
    '        <div class="cta-area">' +
    '          <a href="' + prefilledFormUrl + '" class="btn" target="_blank">Complete Pre-Visit Form</a>' +
    '        </div>' +
    '        ' +
    '        <p>If you have any questions or need to reschedule your appointment, please feel free to reply directly to this email or call our patient care team.</p>' +
    '        <p>Best regards,<br><strong>Clinical Operations Team</strong></p>' +
    '      </div>' +
    '      <div class="footer">' +
    '        <p>This is a secure clinical notification.<br>Need assistance? Please call our patient care team at 610-664-3000.</p>' +
    '      </div>' +
    '    </div>' +
    '  </div>' +
    '</body>' +
    '</html>'
  );
}

/**
 * Builds a professional HTML email requesting missing intake files or identification records
 * from a patient.
 * 
 * @param {string} patientName - Patient's name
 * @param {Array<string>} missingList - Array of missing document names
 * @return {string} HTML email string
 */
function buildPatientMissingDocsEmail(patientName, missingList) {
  var listHtml = '';
  for (var i = 0; i < missingList.length; i++) {
    listHtml += '<li style="margin-bottom: 10px; font-weight: 500; color: #c62828;">' + missingList[i] + '</li>';
  }
  
  return (
    '<!DOCTYPE html>' +
    '<html>' +
    '<head>' +
    '  <meta charset="utf-8">' +
    '  <meta name="viewport" content="width=device-width, initial-scale=1.0">' +
    '  <title>Action Required: Missing Information</title>' +
    '  <style>' +
    '    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f7f8; margin: 0; padding: 0; }' +
    '    .wrapper { width: 100%; background-color: #f5f7f8; padding: 40px 20px; box-sizing: border-box; }' +
    '    .container { max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05); overflow: hidden; border: 1px solid #eef1f2; }' +
    '    .header { background: linear-gradient(135deg, #d32f2f, #b71c1c); padding: 35px 40px; text-align: left; }' +
    '    .header h1 { color: #ffffff; margin: 0; font-size: 24px; font-weight: 700; letter-spacing: -0.5px; }' +
    '    .header p { color: #ffcdd2; margin: 5px 0 0 0; font-size: 14px; }' +
    '    .content { padding: 40px; color: #2d3748; line-height: 1.6; }' +
    '    .content h2 { font-size: 20px; font-weight: 600; color: #b71c1c; margin-top: 0; margin-bottom: 16px; }' +
    '    .content p { font-size: 15px; color: #4a5568; margin-bottom: 24px; }' +
    '    .card { background-color: #ffebee; border-left: 4px solid #d32f2f; padding: 25px; border-radius: 0 8px 8px 0; margin-bottom: 30px; }' +
    '    .card-title { font-weight: bold; color: #b71c1c; font-size: 13px; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.5px; }' +
    '    .footer { background-color: #f8fafc; padding: 30px 40px; text-align: center; border-top: 1px solid #edf2f7; }' +
    '    .footer p { margin: 0; font-size: 13px; color: #718096; line-height: 1.5; }' +
    '    .footer a { color: #d32f2f; text-decoration: none; font-weight: 500; }' +
    '  </style>' +
    '</head>' +
    '<body>' +
    '  <div class="wrapper">' +
    '    <div class="container">' +
    '      <div class="header">' +
    '        <h1>Information Request</h1>' +
    '        <p>Pending Clinical Documentation</p>' +
    '      </div>' +
    '      <div class="content">' +
    '        <h2>Hello ' + patientName + ',</h2>' +
    '        <p>Thank you for initiating your registration. While auditing your new referral file, our administrative team noticed that we are missing a few critical pieces of information required to finalize your chart and confirm authorization.</p>' +
    '        ' +
    '        <div class="card">' +
    '          <div class="card-title">Items Needed for File Completion:</div>' +
    '          <ul style="margin: 0; padding-left: 20px; font-size: 15px;">' +
    '            ' + listHtml +
    '          </ul>' +
    '        </div>' +
    '        ' +
    '        <p><strong>How to submit:</strong> You can reply directly to this email and attach a scanned copy or clear photo of the requested items, or upload them during your online pre-visit registration.</p>' +
    '        <p>Having these documents on file prior to your appointment ensures your billing and insurance claims process smoothly and securely.</p>' +
    '        <p>Warm regards,<br><strong>Clinical Operations Team</strong></p>' +
    '      </div>' +
    '      <div class="footer">' +
    '        <p>This is a secure medical record request.<br>For questions, please call our patient records team at 610-664-3000.</p>' +
    '      </div>' +
    '    </div>' +
    '  </div>' +
    '</body>' +
    '</html>'
  );
}

/**
 * Builds a highly formal and professional HTML email to the patient's legal representative
 * (attorney) requesting litigation status.
 * 
 * @param {string} attorneyName - Attorney's name
 * @param {string} patientName - Patient's name
 * @param {string} accidentDate - Date of injury or accident (optional)
 * @return {string} HTML email string
 */
function buildAttorneyInquiryEmail(attorneyName, patientName, accidentDate) {
  var refLine = accidentDate ? 'Re: ' + patientName + ' | Date of Injury: ' + accidentDate : 'Re: ' + patientName + ' | New Patient Referral';
  
  return (
    '<!DOCTYPE html>' +
    '<html>' +
    '<head>' +
    '  <meta charset="utf-8">' +
    '  <meta name="viewport" content="width=device-width, initial-scale=1.0">' +
    '  <title>Legal-Medical Status Inquiry</title>' +
    '  <style>' +
    '    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: #f5f7f8; margin: 0; padding: 0; }' +
    '    .wrapper { width: 100%; background-color: #f5f7f8; padding: 40px 20px; box-sizing: border-box; }' +
    '    .container { max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05); overflow: hidden; border: 1px solid #eef1f2; }' +
    '    .header { background: linear-gradient(135deg, #1e3a8a, #0f172a); padding: 35px 40px; text-align: left; }' +
    '    .header h1 { color: #ffffff; margin: 0; font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }' +
    '    .header p { color: #93c5fd; margin: 5px 0 0 0; font-size: 13px; font-weight: 500; }' +
    '    .content { padding: 40px; color: #1e293b; line-height: 1.6; }' +
    '    .content h2 { font-size: 16px; font-weight: 700; color: #0f172a; margin-top: 0; margin-bottom: 20px; text-decoration: underline; text-underline-offset: 4px; }' +
    '    .content p { font-size: 15px; color: #334155; margin-bottom: 20px; }' +
    '    .card { background-color: #f8fafc; border: 1px solid #e2e8f0; padding: 20px; border-radius: 8px; margin-bottom: 25px; }' +
    '    .card-title { font-weight: bold; color: #1e3a8a; font-size: 13px; margin-bottom: 8px; text-transform: uppercase; }' +
    '    .footer { background-color: #f8fafc; padding: 30px 40px; text-align: center; border-top: 1px solid #edf2f7; }' +
    '    .footer p { margin: 0; font-size: 12px; color: #64748b; line-height: 1.5; }' +
    '    .footer a { color: #1e3a8a; text-decoration: none; }' +
    '  </style>' +
    '</head>' +
    '<body>' +
    '  <div class="wrapper">' +
    '    <div class="container">' +
    '      <div class="header">' +
    '        <h1>Legal-Medical Coordination Department</h1>' +
    '        <p>' + refLine + '</p>' +
    '      </div>' +
    '      <div class="content">' +
    '        <h2>Dear ' + (attorneyName || 'Counselor') + ',</h2>' +
    '        ' +
    '        <p>Our office has received a medical referral to initiate clinical care for your client, <strong>' + patientName + '</strong>, following their recent injury.</p>' +
    '        ' +
    '        <p>To ensure we coordinate their file correctly, establish appropriate billing accounts, and manage potential medical liens or Letters of Protection (LOP), could you please verify the current legal status of this case?</p>' +
    '        ' +
    '        <div class="card">' +
    '          <div class="card-title">Information Requested:</div>' +
    '          <ol style="margin: 0; padding-left: 20px; font-size: 14.5px; color: #334155;">' +
    '            <li style="margin-bottom: 8px;">Is your client\'s case currently in active litigation, or has the matter been settled or resolved?</li>' +
    '            <li>Are there specific medical record or billing directives our administrative office should follow for your client\'s chart?</li>' +
    '          </ol>' +
    '        </div>' +
    '        ' +
    '        <p>You can quickly respond directly to this email or have your paralegal contact our legal-medical coordinator. We appreciate your assistance in helping us streamline your client\'s healthcare administration.</p>' +
    '        ' +
    '        <p>Sincerely yours,<br><strong>Legal-Medical Coordinator</strong><br>Patient Services Division</p>' +
    '      </div>' +
    '      <div class="footer">' +
    '        <p>CONFIDENTIAL ATTORNEY-CLIENT PRIVILEGED COMMUNICATION.<br>If you are not the intended recipient, please notify us immediately.</p>' +
    '      </div>' +
    '    </div>' +
    '  </div>' +
    '</body>' +
    '</html>'
  );
}

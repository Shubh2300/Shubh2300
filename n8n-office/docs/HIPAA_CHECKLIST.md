# HIPAA Practical Checklist — Self-Hosted PHI Workflow

**Scope:** n8n-office workflow system, self-hosted on the clinic's office Mac, used by Atlantic Pain & Wellness Institute staff. PHI never leaves the office LAN.

**Owner:** Privacy/Security Officer (designate one person — usually the office manager or practice owner).
**Last reviewed:** _______________  **Next review due:** _______________ (annual minimum)

---

## 1. Business Associate Agreements (BAAs)

- [ ] **No BAA needed for this self-hosted system itself.** A BAA is required only when PHI is disclosed to an outside *business associate* (vendor) that creates, receives, maintains, or transmits PHI on your behalf. When the data lives entirely on a machine you own and operate, you are the covered entity — there is no associate to contract with.
- [ ] **BAA still required for every external vendor that touches PHI.** Audit the surrounding stack:
  - [ ] Google Workspace (if Drive/Gmail/Sheets stores any PHI) — sign the Google Workspace BAA in Admin console.
  - [ ] RingCentral (call recordings, faxes, voicemails) — request their BAA.
  - [ ] Any cloud LLM endpoint that sees PHI (OpenAI, Anthropic, Gemini) — **do not send PHI without a signed BAA**. Free-tier APIs (e.g. freellmapi) have no BAA and must not receive PHI.
  - [ ] Backup/sync targets (iCloud, Dropbox, external backup service).
  - [ ] EHR/SIS/clearinghouse vendors.
- [ ] Maintain a one-page **BAA register**: vendor, signed date, scope, renewal date.

## 2. Encryption at Rest

- [ ] **Verify FileVault is ON** on every machine that stores PHI. Run (needs admin password):
  ```
  sudo fdesetup status
  ```
  Expected: `FileVault is On.` If off, enable in System Settings → Privacy & Security → FileVault. Store the recovery key in a sealed envelope in the office safe (not on the same machine).
- [ ] **SQLite databases** (audit log, workflow state, any PHI cache) sit inside the FileVault volume — at-rest encryption is satisfied as long as the disk is locked when the Mac is off/logged out.
- [ ] *Optional, defense-in-depth:* migrate the audit log to **SQLCipher** (AES-256 page-level encryption) so the file is unreadable even if copied off the disk while unlocked. Recommended if the Mac is ever left logged in unattended or if backups go to a non-encrypted target. Not strictly required when FileVault + per-user login is enforced.
- [ ] **Backups are encrypted too.** Time Machine to an encrypted APFS volume, or `restic`/`borg` with a passphrase. An unencrypted backup defeats FileVault.
- [ ] **Removable media policy:** no PHI on unencrypted USB drives. Ever.

## 3. Encryption in Transit

- [ ] **Localhost-only (127.0.0.1) traffic does not require TLS.** When the n8n-office server binds to `127.0.0.1` and the browser connects to `http://localhost:PORT`, packets never leave the loopback interface — there is no wire to sniff. This is the current posture and it is HIPAA-acceptable.
- [ ] **TLS becomes required the moment any of these is true:**
  - [ ] The server binds to `0.0.0.0` or a LAN IP so a second device (tablet, nurse laptop) can reach it across the office Wi-Fi.
  - [ ] A reverse proxy / tunnel exposes the UI outside the host (Tailscale, Cloudflare Tunnel, ngrok, port forward).
  - [ ] Any integration POSTs PHI to a remote URL.
- [ ] **When TLS is required:**
  - [ ] Use a real certificate (mkcert for LAN, or Let's Encrypt via Caddy/Traefik for any externally-reachable hostname).
  - [ ] TLS 1.2 minimum, prefer 1.3. Disable SSLv3/TLS 1.0/1.1.
  - [ ] HSTS on, redirect 80→443.
- [ ] **Office Wi-Fi:** WPA2/WPA3 with a long passphrase. Separate guest SSID with client isolation — patients/guests never share the staff network.

## 4. Audit Log (§164.312(b))

- [ ] **Append-only SQLite table** with columns: `timestamp, actor, intent, target_patient_id_hash, action, result_summary, error_msg, prev_hash, row_hash`. Implementation: `python/integrations/audit_log.py`.
- [ ] **Patient identifiers are hashed** (SHA-256 with a per-install secret pepper) before being written to the log — the log itself is not a PHI dump, and a leaked log file gives an attacker hashes, not names.
- [ ] **Hash-chain for tamper evidence.** Each row's `row_hash = SHA256(prev_hash || canonical(row_fields))`. Any silent edit or deletion breaks the chain. Run `verify_chain()` on a schedule (weekly) and on demand before producing the log for an audit.
- [ ] **Retention: 6 years minimum** from the date of creation OR the date last in effect, whichever is later (45 CFR §164.530(j)(2)). Some states (e.g. NJ for medical records: 7 years; FL: 5 years post-last-visit) require longer — use the longer of state vs. federal.
- [ ] **Operational rules:**
  - [ ] No `UPDATE` or `DELETE` statements against the audit table — only `INSERT`. Enforce in code; periodically `PRAGMA integrity_check`.
  - [ ] Log every PHI read (not just writes). At minimum: which user opened which patient chart, when, from where.
  - [ ] Log every export / print / email that contains PHI.
  - [ ] Log every failed login and every privilege change.
  - [ ] Review logs at least monthly (designate a reviewer, document the review).
- [ ] **Off-host copy.** Nightly export of the audit DB to an encrypted backup so a compromised host cannot erase its own trail.

## 5. Access Control (§164.312(a))

- [ ] **Unique user identity for every person.** No shared "frontdesk" account. Each clinician/staffer logs in as themselves so the audit log names a real human.
- [ ] **Recommended: Google Workspace OAuth** (the practice already has Workspace). Benefits:
  - [ ] Single sign-on with the credentials staff already use.
  - [ ] Centralized offboarding — disable the Workspace user, lose access to n8n-office automatically.
  - [ ] MFA enforced at the Workspace level (require security keys or TOTP for any staffer who touches PHI).
  - [ ] Workspace audit log gives a second, independent record of authentications.
- [ ] **Shared tablet pattern (front desk kiosk):** acceptable only with short auto-lock (≤2 min idle), per-user OAuth login required on each session, and a visible "log out before walking away" reminder. The audit log must still capture the individual user, not the tablet.
- [ ] **Role-based authorization.** Define roles (Front Desk, Nurse, Provider, Biller, Admin) and restrict screens/actions per role. Minimum-necessary access is a HIPAA rule, not a nicety.
- [ ] **Automatic logoff** after idle (10–15 min for workstations, 2 min for shared tablets).
- [ ] **Termination procedure:** documented checklist run within 24 hours of any departure — disable Workspace account, rotate any shared secrets they knew, collect devices.

## 6. Minimum Documentation

- [ ] **Security Risk Analysis (SRA).** Run the free HHS **SRA Tool** (https://www.healthit.gov/topic/privacy-security-and-hipaa/security-risk-assessment-tool) annually and after any material change. Save the PDF output in `docs/sra/YYYY-MM-DD.pdf`. Track each finding to a remediation plan with an owner and due date.
- [ ] **Written Policies & Procedures** (kept current, accessible to staff). Minimum set:
  - [ ] Information Security Policy (overview, scope, roles)
  - [ ] Access Control & Account Management Policy
  - [ ] Audit & Monitoring Policy
  - [ ] Encryption Policy (at rest + in transit)
  - [ ] Workstation & Mobile Device Use Policy
  - [ ] Backup & Disaster Recovery / Contingency Plan
  - [ ] Incident Response & Breach Notification Plan (60-day notification clock, who calls OCR)
  - [ ] Sanction Policy (consequences for staff violations)
  - [ ] BAA Management Policy + register
  - [ ] Workforce Security & Termination Procedures
- [ ] **Workforce training:** every new hire within 30 days, every staffer annually. Keep signed acknowledgments.
- [ ] **Designated officers** (one person can hold both roles in a small clinic):
  - [ ] Privacy Officer (§164.530(a))
  - [ ] Security Officer (§164.308(a)(2))
- [ ] **Notice of Privacy Practices (NPP)** posted in the waiting area and on any patient-facing website. Patients acknowledge receipt on intake.
- [ ] **Retention of documentation: 6 years** for policies, SRAs, training records, incident reports (§164.530(j)).

## 7. Practical Realities for a Small Clinic

- [ ] **Don't over-engineer.** A small practice does not need a SOC team. Aim for: FileVault on, unique logins with MFA, tamper-evident audit log, encrypted backups, an annual SRA, written policies, a BAA register. That covers ~90% of what an OCR investigator will ask for.
- [ ] **Free / low-cost tools that count as real controls:**
  - HHS SRA Tool (free)
  - Google Workspace (already paid) for SSO + MFA + audit
  - FileVault (free, built in)
  - Time Machine on an encrypted disk (free)
  - SQLCipher (free, drop-in for sqlite3)
- [ ] **Document what you do.** The single most common OCR finding against small practices is "no documentation," not "no controls." If FileVault is on but you can't show the policy that says it must be on and the date you last verified, you fail the audit. Keep a one-page log of monthly checks.
- [ ] **Patch monthly.** macOS updates, n8n updates, Python deps. Track in a one-line monthly log.
- [ ] **Breach math: any unauthorized disclosure of unsecured PHI is presumed a breach** unless a 4-factor risk assessment shows low probability of compromise. *Encrypted* PHI (FileVault on, disk locked) is *secured* under the HHS guidance — losing a powered-off encrypted laptop is generally not a reportable breach. This is why FileVault matters more than any other single control.
- [ ] **Vendor minimization.** Every BAA is a relationship to manage. Prefer self-hosted for PHI workflows when you can.
- [ ] **Incident drill once a year.** Tabletop: "front desk tablet was stolen — what do we do in the next 60 minutes / 24 hours / 60 days?" Document the drill.
- [ ] **What you can skip / defer:** SIEM, dedicated DLP, formal penetration tests, ISO 27001. None of these are required by HIPAA for a single-office practice. Revisit if/when you go multi-site or commercial.

---

## Quick Self-Check (run monthly, ≤5 min)

- [ ] `sudo fdesetup status` → "FileVault is On."
- [ ] Last Time Machine backup completed within 7 days, target volume is encrypted.
- [ ] Audit-log `verify_chain()` returns OK.
- [ ] No accounts in Google Workspace for ex-staff.
- [ ] No PHI in any folder synced to a non-BAA cloud (Drive root, iCloud Desktop, Dropbox).
- [ ] All Macs are on a supported macOS version with this month's security update.

---
*This checklist summarizes the HIPAA Security Rule (45 CFR Part 164, Subparts C and E) for a small self-hosted setup. It is not legal advice. When in doubt, consult counsel.*

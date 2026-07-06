# RingCentral Integration — Setup Guide

Goal: after every phone call, the call log and its AI summary land on the
patient's record automatically (`ringcentral_sync.py` does this; the dashboard
shows it in the patient drawer notes).

The same RingCentral app also powers the daily fax digest
(`ringcentral_fax_sync.py`): inbound faxes are downloaded, summarized, matched
to patients when possible, and queued for staff review.

Being signed into the RingCentral desktop app does **not** give the API
access — the API uses its own server credential (a JWT) created once in the
RingCentral Developer Console. ~15 minutes, no code.

## One-time setup (needs RingCentral admin or a developer-console invite)

1. **Open the Developer Console:** https://developers.ringcentral.com → Sign in
   with the clinic's RingCentral account → **Console → Apps → Register App**.
2. **App type:** choose **"REST API App"**, auth type **"JWT auth flow"**
   (server/no UI).
3. **Permissions (scopes):** add at minimum:
   - `ReadCallLog` (call history)
   - `ReadMessages` (SMS/message store + inbound fax records and attachments)
   - `ReadAccounts`
   - If the account has RingSense / AI features: the RingSense read scope
     shown in the console (name varies by product, e.g. `RingSense`).
   - If call recordings are needed: `ReadCallRecording`.
4. Note the app's **Client ID** and **Client Secret**.
5. **Create the JWT credential:** Developer Console → top-right profile menu →
   **Credentials → Create JWT**. Authorize it for the app you just registered
   (or "all apps"), environment **Production**. Copy the long JWT string.
6. **Put all three in `.env`** (never in code):

   ```
   RC_SERVER_URL=https://platform.ringcentral.com
   RC_CLIENT_ID=<from step 4>
   RC_CLIENT_SECRET=<from step 4>
   RC_JWT=<from step 5>
   ```

7. **Graduate the app to Production** if the console created it in Sandbox
   (Apps → your app → "Apply for Production"). Call-log reads are usually
   auto-approved.

## Run it

```bash
python3 ringcentral_sync.py            # one pass, last 24 hours
python3 ringcentral_sync.py --hours 168  # backfill a week
python3 ringcentral_sync.py --loop     # keep polling every 5 minutes
python3 ringcentral_fax_sync.py        # pull today's inbound faxes into the digest
python3 ringcentral_fax_sync.py --days 7 # backfill inbound faxes for a week
```

What it does each pass:
- Pulls the company call log (detailed view).
- Matches each call's other-party phone number against patient phone numbers
  in `patient_database.json` (last-10-digit match).
- Appends matched calls to the patient's record (`calls` + a note in
  `notes_summaries`, so the drawer/timeline shows it) and everything to
  `calls.json`.
- Tries to fetch the RingSense AI summary per call; if the plan doesn't have
  RingSense API access it degrades gracefully to the plain call log.

What the fax digest does each pass:
- Pulls inbound fax messages from the RingCentral message store.
- Downloads the first fax attachment to
  `~/.gemini/antigravity/scratch/faxes/YYYY-MM-DD/`.
- Extracts text using best-effort PDF text extraction, with OCR if `tesseract`
  is installed.
- Classifies the fax (referral, EOB/payment, denial, prior auth, records,
  attorney/legal, refill, or unknown).
- Matches to a patient when the text contains reliable identifiers.
- Stores the digest in `fax_digest.sqlite3` and creates a review task when
  action is needed.

## Notes / later

- **Webhooks (true real-time):** polling every 5 min is the right v1 on a
  laptop. When the app moves to a machine with a public HTTPS URL, switch to
  the Subscription API (`/restapi/v1.0/subscription`) with a webhook delivery
  address — same data, zero lag.
- **Unmatched calls** (no patient with that number) still land in
  `calls.json` marked `"patient_name": "Not matched"` — the reconciliation
  agent can surface these for manual filing.
- **RingSense endpoint shape** may differ by product tier; if summaries come
  back empty on a plan that has them, check the account's product name at
  https://developers.ringcentral.com/ringsense-api and adjust
  `fetch_ringsense_summary()`.
- **Compliance:** confirm the BAA covers API/RingSense data export, and check
  call-recording consent rules for your state before storing transcripts.

Sources: [JWT quick start](https://developers.ringcentral.com/guide/authentication/jwt/quick-start),
[JWT auth flow](https://developers.ringcentral.com/guide/authentication/jwt-flow),
[Webhooks](https://developers.ringcentral.com/guide/notifications/webhooks/creating-webhooks),
[RingSense API](https://developers.ringcentral.com/ringsense-api).

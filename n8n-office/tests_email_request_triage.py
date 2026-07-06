#!/usr/bin/env python3
"""tests_email_request_triage.py — offline unit checks for the EMAIL request
detector's classifier.

Pure-logic tests on crafted strings: no DB, no server, no network. We exercise
``email_request_triage.classify`` directly (the pure function the live scan and
the read-only dry-run both call) to prove the precision filter behaves:

  * real records / scheduling / billing asks are CARDED with the right intent;
  * the excluded categories (newsletters, marketing, the app's own Intake
    Digest / Alerts, delivery/receipt bots, out-of-office auto-replies, pure
    thank-you replies) are NEVER carded;
  * the safety invariant: classifying a records request only labels it — the
    detector has no send path at all (asserted by the module surface).

Exit 0 iff every assertion holds; prints a machine-readable SUMMARY line.

Run:  cd /Users/shubh/n8n-office && python3 tests_email_request_triage.py
"""
from __future__ import annotations

import sys

from app import email_request_triage as t

_passed = 0
_failed = 0


def check(name: str, cond: bool) -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failed += 1
        print(f"FAIL  {name}")


def carded(sender: str, subject: str, body: str) -> tuple[bool, str]:
    r = t.classify(sender, subject, body)
    return r["carded"], r["intent"]


# ---------------------------------------------------------------------------
# 1. RECORDS-RELEASE — the biggest real category.
# ---------------------------------------------------------------------------
ok, intent = carded(
    "Zena Feldman <Zena@mylosflaw.com>",
    "REQ: Alonya Dorsey - Medical records",
    "Please send all medical records for the client Alonya Dorsey, DOB 3/2/1990.")
check("law-firm records request -> carded", ok)
check("law-firm records request -> records_request", intent == "records_request")

ok, intent = carded(
    "Aleya Jones <ajones@excelsiainjury.com>",
    "D.Ryndycz 7/24 Booking Packet",
    "Attached is the booking packet. Please send the operative notes and MRI's.")
check("op-notes/MRI ask -> records_request", ok and intent == "records_request")

# Law-firm sender + DOB line with NO explicit actionable phrase -> the
# records_request fallback fires (the dominant real category for such mail).
ok, intent = carded(
    "Annette Gianfelice <annette@mrgordonlaw.com>",
    "OUR CLIENT: ARIF PRICE, D/BIRTH: 8/30/79",
    "Good morning. Attached is the correspondence regarding the above matter.")
check("law-firm + bare DOB (no phrase) -> records_request fallback",
      ok and intent == "records_request")

# Same firm + DOB but WITH an explicit 'reach out' ask -> other_actionable
# (an explicit phrase outranks the bare-DOB fallback). Still carded, still a
# valid actionable intent.
ok, intent = carded(
    "Annette Gianfelice <annette@mrgordonlaw.com>",
    "OUR CLIENT: ARIF PRICE, D/BIRTH: 8/30/79",
    "Good morning: Attached please find our correspondence. Please reach out.")
check("law-firm + DOB + 'reach out' -> other_actionable",
      ok and intent == "other_actionable")

# ---------------------------------------------------------------------------
# 2. BILLING.
# ---------------------------------------------------------------------------
ok, intent = carded(
    "Sid Jackson <sid@mdmanage.com>",
    "Re: ITEMIZED BILL",
    "Hi, yes please send the itemized bill for MLS148. Thank you.")
check("itemized bill -> carded", ok)
check("itemized bill -> billing", intent == "billing")

ok, intent = carded(
    "Lina Park <lina@mdmanage.com>",
    "RE: HCFA form",
    "Attached HCFA form for the patient, please review the billing statement.")
check("HCFA/billing statement -> billing", ok and intent == "billing")

# ---------------------------------------------------------------------------
# 3. SCHEDULING (weak signal + clinical/legal context required).
# ---------------------------------------------------------------------------
ok, intent = carded(
    "Karina Medynska <kmedynska@mylosflaw.com>",
    "Damon Holden - REQ for Appointment",
    "Would it be possible to schedule Mr. Holden for pain management? DOB 1/1/80")
check("law-firm appointment request -> carded", ok)
check("appointment request -> scheduling", intent == "scheduling")

# ---------------------------------------------------------------------------
# 4. EXCLUSIONS — none of these may ever be carded.
# ---------------------------------------------------------------------------
# 4a. Newsletter / substack (even though it says "schedule").
ok, _ = carded(
    "Washington Policy Review <washingtonpolicyreview@substack.com>",
    "Washington Policy Review: June 25, 2026",
    "View this post on the web. Schedule time to read. Unsubscribe here.")
check("substack newsletter -> excluded", not ok)

# 4b. Marketing blast with a request word.
ok, _ = carded(
    '"GoPro" <gopro@e-mail.gopro.com>',
    "Stock Up on Gear + Save $30!",
    "Exclusive offer! Please send us your order. 30% off. Unsubscribe.")
check("marketing blast -> excluded", not ok)

# 4c. The app's OWN Intake Digest (self-notification).
ok, _ = carded(
    "mainlinesurgery@gmail.com",
    "[Intake] 75 item(s) need review",
    "Atlantic Pain & Wellness - Intake Digest. 75 item(s) need review. Please review.")
check("own Intake Digest -> excluded", not ok)

# 4d. The app's own Intake Alert booking-request notification.
ok, _ = carded(
    "mainlinesurgery@gmail.com",
    "[Intake Alert] Booking request: D. Ryndycz",
    "A patient asked to schedule an appointment. Patient: D. Ryndycz")
check("own Intake Alert (schedule words) -> excluded", not ok)

# 4e. Delivery / fax receipt bot.
ok, _ = carded(
    "RingCentral <notify@ringcentral.com>",
    "New Fax Message from (307) 410-2307",
    "You have a new fax. Please review the attached document in your account.")
check("RingCentral fax receipt -> excluded", not ok)

# 4f. Out-of-office auto-reply (even about billing).
ok, _ = carded(
    "Vincent Lee <bkishan@mdmanage.com>",
    "Automatic reply: MAIN LINE - MLS79",
    "I am currently out of the office. Please send your records request later.")
check("out-of-office auto-reply -> excluded", not ok)

# 4g. Pure thank-you reply, no ask.
ok, _ = carded(
    "Cathy Sola <cathy@jfinelaw.com>",
    "RE: Asad Grant",
    "Thank you!")
check("thanks-only reply -> excluded", not ok)

# 4h. Building-management COI reminder (has "please send" but no patient/case).
ok, _ = carded(
    "Laura Pinyard <lpinyard@balaplazamgmt.com>",
    "RE: COI",
    "Hi Roha, Please send the sample to your insurance company. We need to be listed.")
check("building-mgmt COI -> excluded", not ok)

# 4i. HR / credentialing thread that merely quotes the practice name.
ok, _ = carded(
    "Samantha Ball <sball@naspacmd.com>",
    "RE: Dr. Fenil Gandhi - Application Status",
    "Do you have a general timeline for approval? North American Spine & Pain, "
    "Main Line Surgical Center is on file.")
check("HR credentialing (quoted practice name) -> excluded", not ok)

# ---------------------------------------------------------------------------
# 5. SAFETY INVARIANT — the detector cannot send anything.
# ---------------------------------------------------------------------------
# The module must expose NO send/EMR surface — it only stages cards. A records
# request classification returns a label, never triggers a send.
check("no send_email symbol on detector module",
      not hasattr(t, "send_email"))
check("no send_records / release symbol on detector module",
      not hasattr(t, "send_records") and not hasattr(t, "release_records"))
res = t.classify("Zena Feldman <Zena@mylosflaw.com>",
                 "REQ: Records - Carlos Pedroza", "Please send all records.")
check("records classify returns label only (dict, no side effect)",
      isinstance(res, dict) and res["carded"] and "intent" in res)

# ---------------------------------------------------------------------------
# 6. dry_run is read-only and well-shaped (schema keys present).
# ---------------------------------------------------------------------------
# Monkeypatch comms.list_messages so no DB is touched; assert dry_run writes
# nothing (it has no enqueue/update calls) and returns the documented shape.
from app import comms as _comms  # noqa: E402

_fake_rows = [
    {"direction": "in", "sender": "Zena@mylosflaw.com",
     "subject": "REQ: Records - X", "body": "Please send all medical records."},
    {"direction": "in", "sender": "gopro@e-mail.gopro.com",
     "subject": "Save $30", "body": "Exclusive offer. Unsubscribe."},
    {"direction": "out", "sender": "us", "subject": "reply", "body": "sent"},
]
_orig = _comms.list_messages
_comms.list_messages = lambda channel=None, status=None, limit=50: list(_fake_rows)
try:
    rep = t.dry_run()
finally:
    _comms.list_messages = _orig
check("dry_run scans inbound only (out skipped)",
      rep["total_email_scanned"] == 2)
check("dry_run would_card == 1", rep["would_card"] == 1)
check("dry_run by_intent records_request == 1",
      rep["by_intent"].get("records_request") == 1)
for key in ("total_email_scanned", "would_card", "by_intent",
            "sample_carded", "sample_excluded"):
    check(f"dry_run has key {key}", key in rep)

print(f"SUMMARY {_passed} passed, {_failed} failed")
sys.exit(0 if _failed == 0 else 1)

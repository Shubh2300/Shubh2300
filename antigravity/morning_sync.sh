#!/bin/bash
# Morning clinical sync wrapper.
# The real orchestration lives in daily_clinical_sync.py so the dashboard,
# launchd/Task Scheduler, and manual terminal runs all produce the same status.

cd "$(dirname "$0")"
python3 "$(dirname "$0")/intake_queue_sync.py" || echo "intake queue sync failed (non-fatal)"
python3 daily_clinical_sync.py "$@"

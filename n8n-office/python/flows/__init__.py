"""n8n-office flow modules.

One module per end-to-end workflow (patient lookup, file request, appointment,
general message). Each module exposes a ``run(payload: dict) -> dict``
entrypoint that an n8n "Execute Command" node (or a direct Python caller)
invokes. Flows compose adapters from ``integrations/`` and never re-implement
EMR scrapers or SMS auth.

Read-only by default. Any write operation is staged as a stub + staff-approval
gate per Q5; see ``appointment.py`` for the canonical pattern.
"""

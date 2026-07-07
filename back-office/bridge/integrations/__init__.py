"""Vendored EMR integration modules for the Local EMR Bridge.

These modules are copied verbatim (with a provenance header) from the proven
n8n-office integration stack. They are the ONLY code in the bridge that talks
to a real EMR. See each file's header for its source path.

Import note: ``emr_session_manager`` inserts this directory onto ``sys.path``
so its sibling imports (``from sis_client import ...``) resolve whether the
package is imported as ``bridge.integrations`` or the directory is on the path
directly.
"""

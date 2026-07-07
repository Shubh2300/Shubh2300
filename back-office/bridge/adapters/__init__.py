"""Adapter layer: the ONLY code that decides, at runtime, whether an Action-
Registry action can genuinely execute against a real EMR, and that wraps the
vendored clients where a real implementation exists. See _common.py for the
gate order (implementation status -> credentials -> patient match)."""

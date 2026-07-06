"""Back-office assistant package.

TLS trust bootstrap
-------------------
Some Python builds (notably the python.org macOS framework builds) ship
without a usable system CA bundle, so ``ssl.create_default_context()`` — used
transitively by ``urllib`` (RingCentral), ``imaplib`` (Gmail), and ``requests``
— cannot verify server certificates and every outbound HTTPS call fails with
``CERTIFICATE_VERIFY_FAILED``.

We point Python's default SSL machinery at ``certifi``'s bundle by exporting
``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` into the process environment before
any TLS context is created. ``setdefault`` means an operator-provided value
(e.g. an OS-specific bundle set in the launchd/systemd unit) always wins, and
the whole thing is wrapped so a missing ``certifi`` never breaks import — the
app simply falls back to the platform default. Portable across machines:
``certifi.where()`` resolves wherever it happens to be installed.
"""

from __future__ import annotations

import os as _os

try:  # pragma: no cover - environment bootstrap, exercised at process start
    import certifi as _certifi

    _bundle = _certifi.where()
    _os.environ.setdefault("SSL_CERT_FILE", _bundle)
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
except Exception:  # certifi absent / unreadable — keep platform default trust
    pass

del _os

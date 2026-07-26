"""
Standalone session context shim for the Hermes Chess plugin.

When running inside the Hermes gateway, this imports the real
gateway.session_context. When installed standalone (e.g. on Bean),
get_session_env returns the relevant environment variable or None.
"""

import os

# When installed inside Hermes plugins/ dir, the gateway package is
# importable via sys.path manipulation by the Hermes loader.
# When installed as a standalone package, we provide a local fallback.
try:
    from gateway.session_context import get_session_env as _get_session_env
    _has_hermes = True
except (ImportError, ModuleNotFoundError):
    _has_hermes = False


def get_session_env(key: str, default=None):
    """Get a per-conversation session environment variable.

    Inside Hermes gateway this delegates to the real session context.
    Standalone it falls back to os.environ.
    """
    if _has_hermes:
        return _get_session_env(key, default=default)
    return os.environ.get(key, default)

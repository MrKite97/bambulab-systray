"""Secure storage of the Bambu access token via keyring (Windows DPAPI).

On Windows, keyring's default backend is ``Windows.WinVaultKeyring`` -> the
Windows Credential Locker, encrypted per-user via DPAPI. We store ONLY the
access token here -- never the account password, and never a plaintext file.
The token grants full account access, so plaintext-on-disk is a credential-leak
risk (RESEARCH Security Domain, threat T-01-01).

On token expiry (a 401 on connect) the caller clears the token and re-runs the
full login flow; there is no silent refresh (the Bambu refresh endpoint is dead).
"""

import keyring
import keyring.errors

SERVICE = "BambuLabSystray"
_KEY = "access_token"


def save_token(token: str) -> None:
    """Persist the access token in the OS credential store (DPAPI on Windows)."""
    keyring.set_password(SERVICE, _KEY, token)


def load_token() -> str | None:
    """Return the stored access token, or None if none is stored."""
    return keyring.get_password(SERVICE, _KEY)


def clear_token() -> None:
    """Delete the stored access token. Idempotent -- absent key is not an error."""
    try:
        keyring.delete_password(SERVICE, _KEY)
    except keyring.errors.PasswordDeleteError:
        pass

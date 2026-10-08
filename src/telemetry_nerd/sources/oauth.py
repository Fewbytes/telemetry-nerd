"""OAuth2 Authorization Code + PKCE support, shared by PromQLSource, ElasticsearchSource,
and grafana.py in place of AuthRef's static secret for sources behind interactive SSO.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from telemetry_nerd.sources.base import SourceError

_EXPIRY_MARGIN_S = 60


class OAuthLoginTimeout(SourceError):
    pass


class OAuthLoginFailed(SourceError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636, S256 method."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def generate_state() -> str:
    return _b64url(secrets.token_bytes(16))


@dataclass(frozen=True)
class TokenState:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds


def token_path(data_dir: Path, name: str) -> Path:
    d = Path(data_dir) / "oauth-tokens"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def load_token(path: Path) -> TokenState | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        return TokenState(**raw)
    except TypeError:
        return None


def save_token(path: Path, state: TokenState) -> None:
    path.write_text(json.dumps(asdict(state)))
    path.chmod(0o600)

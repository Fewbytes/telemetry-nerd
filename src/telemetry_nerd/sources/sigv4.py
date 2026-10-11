"""AWS SigV4 request signing for Amazon OpenSearch Service (service `es`).

Unlike AuthRef's bearer/basic/apikey schemes, a SigV4 signature is not a static header: it
depends on the method, url, headers and body of each individual request, and credentials come
from the standard AWS chain (env vars, shared config/credentials files, or an instance/task
role) rather than this project's single-secret env/file model. `Signer` resolves that chain once
and signs each outgoing request with it.
"""

from __future__ import annotations

from collections.abc import Mapping

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import Session

from telemetry_nerd.sources.spec import AuthRef, MissingSecret

#: Amazon OpenSearch Service's SigV4 service name (distinct from "opensearchserverless")
SERVICE = "es"


class Signer:
    """Signs requests for one (region, credentials-chain) pair. Build once per source; call
    `sign` per request, since credentials can be temporary and rotate."""

    def __init__(self, region: str, credentials: Credentials) -> None:
        self._region = region
        self._credentials = credentials

    @classmethod
    def from_auth(cls, auth: AuthRef) -> Signer:
        assert auth.scheme == "sigv4" and auth.region is not None
        credentials = Session().get_credentials()
        if credentials is None:
            raise MissingSecret(
                "no AWS credentials found for scheme='sigv4'",
                hint="export AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN if "
                "temporary), set up ~/.aws/credentials, or run the daemon with an instance/task "
                "role that can reach OpenSearch",
            )
        return cls(auth.region, credentials)

    def sign(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """SigV4's Authorization/X-Amz-Date(/X-Amz-Security-Token), to merge into `headers`.
        `params` are the request's query parameters, signed as part of the canonical request -
        pass the same ones given to the HTTP client so the signature matches what is sent."""
        frozen = self._credentials.get_frozen_credentials()
        request = AWSRequest(
            method=method, url=url, headers=dict(headers), data=body,
            params=dict(params) if params else None,
        )  # fmt: skip
        SigV4Auth(frozen, SERVICE, self._region).add_auth(request)
        return dict(request.headers)

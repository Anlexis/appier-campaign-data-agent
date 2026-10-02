"""AgentCore Platform v1.0 - Appier campaign-management API client.

Service layer: a thin wrapper around the Appier (advertising platform)
campaign-management REST endpoints. Contains NO business logic, NO routing,
and NO credentials - the integration key is passed in per call by the node
(which reads it from the invocation context). This module imports no
framework/SDK internals - pure stdlib.

v1 LIMITATION (deliberate, documented):
    The DEFAULT transport is a deterministic, NETWORK-FREE stub. It returns the
    documented Appier response shapes (a ``campaign`` object for lookups; an
    update-receipt shape with a ``campaign_id`` echo for setting/status writes,
    derived from the request) so the pipeline is runnable and testable without
    a live Appier tenant or an HTTP client library - it does NOT perform a live
    Appier call. The limitation is documented rather than papered over with a
    fake live call.

    To perform real Appier calls, inject live transports at construction time;
    the method contracts follow the Appier campaign-management REST shape
    (GET /campaigns/{id}, PATCH /campaigns/{id}, PATCH /campaigns/{id}/status),
    so no business-logic change is needed to go live. A live transport also
    requires a real integration key (see CallAppierApiNode - the stub runs
    without one because no request ever leaves the process).

Transport contract:
    ``(url, headers, json_body, timeout_s) -> (status_code, response_dict)``

    The deadline is part of the contract rather than a caller-side detail: an
    outbound integration without one blocks the whole invoke, and the value is
    a deployment setting (``timeout_s`` in ``config/config.yaml``) that has to
    reach the request that uses it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

# A transport callable: (url, headers, json_body, timeout_s) -> (status, response_dict)
Transport = Callable[[str, dict[str, str], dict[str, Any], float], "tuple[int, dict[str, Any]]"]

_BASE_URL = "https://api.appier.com/v1"
DEFAULT_TIMEOUT_S = 30.0


class AppierApiError(Exception):
    """Raised when the Appier API returns a non-2xx status."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Appier API error {status_code}: {message}")


class AppierClient:
    """Appier campaign-management API client.

    Args:
        base_url: Appier API base URL (default https://api.appier.com/v1).
        timeout_s: per-request deadline handed to the transport.
        patch/get: optional injected transports (tests or a live client).
            When none is injected, a deterministic NETWORK-FREE v1 stub is used
            (see the module docstring - it returns the documented shape without
            a live Appier call).
    """

    def __init__(
        self,
        base_url: str = _BASE_URL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        patch: Transport | None = None,
        get: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._patch = patch
        self._get = get

    # -- transport mode --------------------------------------------------------

    @property
    def uses_stub_transport(self) -> bool:
        """True when NO live transport is injected (the network-free v1 default)."""
        return self._patch is None and self._get is None

    @property
    def timeout_s(self) -> float:
        """The per-request deadline this client passes to its transport."""
        return self._timeout_s

    # -- auth ----------------------------------------------------------------

    def _headers(self, api_key: str) -> dict[str, str]:
        """Build the Appier API auth headers.

        api_key is supplied per-call by the node; it is never persisted on the
        instance or logged.
        """
        return {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        }

    # -- v1 deterministic stub transport (default; NO network) ----------------

    def _stub_transport(
        self, url: str, headers: dict[str, str], json_body: dict[str, Any], timeout_s: float
    ) -> "tuple[int, dict[str, Any]]":
        """Deterministic, network-free v1 stub - returns the documented Appier shape.

        NOT a live call. Synthetic values are derived from the request so the
        response is stable and inspectable. See the module docstring for the v1
        limitation and how to inject live transports.
        """
        seed = url + "|" + json.dumps(json_body, sort_keys=True, ensure_ascii=False, default=str)
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if json_body.get("_appier_op") == "lookup":
            campaign_id = str(json_body.get("campaign_id", "")) or f"c-{digest[:8]}"
            # Documented GET /campaigns/{id} shape: {"campaign": {...}}
            return 200, {
                "campaign": {
                    "id": campaign_id,
                    "name": f"Campaign {campaign_id}",
                    "status": "active",
                    "daily_budget": "0",
                    "schedule": {"start_date": "", "end_date": ""},
                    "settings": [],
                },
                "_stub": True,  # marks the network-free v1 stub response
            }
        # PATCH /campaigns/{id} (update) / PATCH /campaigns/{id}/status -
        # update-receipt shape with a campaign_id echo so the caller can
        # reference the affected campaign without a follow-up lookup.
        campaign_id = str(json_body.get("campaign_id", "")) or f"c-{digest[:8]}"
        updated = json_body.get("campaign") or {}
        receipt: dict[str, Any] = {
            "campaign_id": campaign_id,
            "updated_fields": sorted(updated.keys()),
            "_stub": True,  # marks the network-free v1 stub response
        }
        if "status" in json_body:
            receipt["status"] = json_body["status"]
        return 200, receipt

    def _resolve(self, injected: Transport | None) -> Transport:
        return injected or self._stub_transport

    # -- public API ---------------------------------------------------------

    def get_campaign(self, campaign_id: str, api_key: str) -> dict[str, Any]:
        """GET /campaigns/{id} - look up a campaign's settings by campaign id.

        Returns the parsed response dict (containing ``campaign``). Raises
        AppierApiError on non-2xx.
        """
        url = f"{self._base_url}/campaigns/{campaign_id}"
        transport = self._resolve(self._get)
        status, body = transport(
            url,
            self._headers(api_key),
            {"_appier_op": "lookup", "campaign_id": campaign_id},
            self._timeout_s,
        )
        if not (200 <= status < 300):
            raise AppierApiError(status, _err_message(body))
        return body

    def update_campaign(self, campaign_id: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        """PATCH /campaigns/{id} - partially update a campaign's settings.

        ``payload`` is the ``{"campaign": {...}}`` request body (budget /
        schedule / name / settings). Returns the parsed response dict (update
        receipt). Raises AppierApiError on a non-2xx status.
        """
        url = f"{self._base_url}/campaigns/{campaign_id}"
        transport = self._resolve(self._patch)
        body_out: dict[str, Any] = {"campaign_id": campaign_id}
        body_out.update(payload)
        status, body = transport(url, self._headers(api_key), body_out, self._timeout_s)
        if not (200 <= status < 300):
            raise AppierApiError(status, _err_message(body))
        return body

    def set_campaign_status(self, campaign_id: str, target_status: str, api_key: str) -> dict[str, Any]:
        """PATCH /campaigns/{id}/status - pause or (re)activate a campaign.

        ``target_status`` is "paused" or "active". Returns the parsed response
        dict (update receipt). Raises AppierApiError on a non-2xx status.
        """
        url = f"{self._base_url}/campaigns/{campaign_id}/status"
        transport = self._resolve(self._patch)
        status, body = transport(
            url,
            self._headers(api_key),
            {"campaign_id": campaign_id, "status": target_status},
            self._timeout_s,
        )
        if not (200 <= status < 300):
            raise AppierApiError(status, _err_message(body))
        return body


def _err_message(body: Any) -> str:
    """Extract a human-readable error message from an Appier error body."""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e) for e in errors)
        msg = body.get("message")
        if msg:
            return str(msg)
    return str(body)

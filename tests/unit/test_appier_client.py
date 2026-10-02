# CMN-C2-289 - Unit tests: AppierClient service (Appier campaign-management REST shape).
# Pure service layer (stdlib-only, no framework imports) - plain function tests.
#
# Transport contract: (url, headers, json_body, timeout_s) -> (status, body).
# The deadline is part of the contract, so every injected transport receives it
# and the tests assert the configured value actually arrives.

import pytest

from src.services.appier_client import DEFAULT_TIMEOUT_S, AppierApiError, AppierClient


def test_get_campaign_success_with_injected_get():
    captured = {}

    def get(url, headers, body, timeout_s):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        captured["timeout_s"] = timeout_s
        return 200, {"campaign": {"id": "1001", "name": "Campaign 1001", "status": "active"}}

    client = AppierClient("https://appier.example.test/v1/", timeout_s=12, get=get)
    resp = client.get_campaign("1001", "key123")
    assert resp["campaign"]["id"] == "1001"
    assert captured["url"] == "https://appier.example.test/v1/campaigns/1001"
    # Appier campaign-API auth: the per-call key travels in X-Api-Key.
    assert captured["headers"]["X-Api-Key"] == "key123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["body"]["campaign_id"] == "1001"
    # The configured deadline reaches the request, not just the constructor.
    assert captured["timeout_s"] == 12


def test_update_campaign_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body, timeout_s):
        captured["url"] = url
        captured["body"] = body
        captured["timeout_s"] = timeout_s
        return 200, {"campaign_id": "1001", "updated_fields": ["daily_budget"]}

    client = AppierClient("https://appier.example.test/v1", patch=patch)
    payload = {"campaign": {"daily_budget": "3000"}}
    resp = client.update_campaign("1001", payload, "key")
    assert resp["campaign_id"] == "1001"
    assert captured["url"] == "https://appier.example.test/v1/campaigns/1001"
    # The client injects the campaign_id echo alongside the request payload.
    assert captured["body"] == {"campaign_id": "1001", "campaign": {"daily_budget": "3000"}}
    # Unset on the client -> the documented default deadline.
    assert captured["timeout_s"] == DEFAULT_TIMEOUT_S


def test_set_campaign_status_success_with_injected_patch():
    captured = {}

    def patch(url, headers, body, timeout_s):
        captured["url"] = url
        captured["body"] = body
        captured["timeout_s"] = timeout_s
        return 200, {"campaign_id": "1001", "status": "paused"}

    client = AppierClient("https://appier.example.test/v1", patch=patch)
    resp = client.set_campaign_status("1001", "paused", "key")
    assert resp["campaign_id"] == "1001"
    assert captured["url"] == "https://appier.example.test/v1/campaigns/1001/status"
    assert captured["body"] == {"campaign_id": "1001", "status": "paused"}
    assert captured["timeout_s"] == DEFAULT_TIMEOUT_S


def test_non_2xx_raises_appier_api_error():
    def get(url, headers, body, timeout_s):
        return 404, {"errors": ["campaign not found"]}

    client = AppierClient("https://appier.example.test/v1", get=get)
    with pytest.raises(AppierApiError) as exc:
        client.get_campaign("9999", "key")
    assert exc.value.status_code == 404
    assert "campaign not found" in str(exc.value)


def test_non_2xx_message_key_fallback():
    def patch(url, headers, body, timeout_s):
        return 400, {"message": "budget is malformed"}

    client = AppierClient("https://appier.example.test/v1", patch=patch)
    with pytest.raises(AppierApiError) as exc:
        client.update_campaign("1001", {"campaign": {"daily_budget": "x"}}, "key")
    assert "budget is malformed" in str(exc.value)


def test_default_stub_transport_lookup_shape():
    # No transport injected -> deterministic, network-free v1 stub.
    client = AppierClient()
    assert client.uses_stub_transport is True
    resp = client.get_campaign("1001", "key")
    assert resp.get("_stub") is True
    record = resp["campaign"]
    assert record["id"] == "1001"
    assert record["name"] == "Campaign 1001"
    assert record["status"] == "active"


def test_default_stub_transport_update_echoes_campaign_id():
    client = AppierClient()
    resp = client.update_campaign("1001", {"campaign": {"daily_budget": "3000", "name": "Summer"}}, "key")
    assert resp.get("_stub") is True
    assert resp["campaign_id"] == "1001"
    assert resp["updated_fields"] == ["daily_budget", "name"]


def test_default_stub_transport_status_receipt():
    client = AppierClient()
    resp = client.set_campaign_status("1001", "paused", "key")
    assert resp.get("_stub") is True
    assert resp["campaign_id"] == "1001"
    assert resp["status"] == "paused"


def test_injected_transport_disables_stub_flag():
    client = AppierClient(get=lambda url, headers, body, timeout_s: (200, {"campaign": {}}))
    assert client.uses_stub_transport is False


def test_timeout_defaults_and_is_exposed():
    assert AppierClient().timeout_s == DEFAULT_TIMEOUT_S
    assert AppierClient(timeout_s=5).timeout_s == 5.0

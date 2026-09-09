import httpx
import pytest

from bot import market


class _Response:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self) -> dict:
        return self._payload


def test_funding_defaults_to_okx(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs) -> _Response:
        calls.append(url)
        assert kwargs["params"] == {"instId": "BTC-USDT-SWAP"}
        return _Response(200, {"data": [{"fundingRate": "0.00012"}]})

    monkeypatch.setattr(market.httpx, "get", fake_get)

    rate, provider, attempted, failures = market._fetch_funding_rate("cbBTC", 2)

    assert rate == pytest.approx(0.012)
    assert provider == "okx"
    assert attempted == ("okx",)
    assert failures == ()
    assert calls == [market.OKX_FUNDING]


def test_funding_falls_back_after_blocked_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs) -> _Response:
        del kwargs
        if url == market.BINANCE_PREMIUM:
            return _Response(451, {})
        if url == market.BYBIT_TICKERS:
            return _Response(403, {})
        assert url == market.OKX_FUNDING
        return _Response(200, {"data": [{"fundingRate": "-0.0002"}]})

    monkeypatch.setattr(market.httpx, "get", fake_get)

    rate, provider, attempted, failures = market._fetch_funding_rate(
        "cbBTC", 2, ("binance", "bybit", "okx")
    )

    assert rate == pytest.approx(-0.02)
    assert provider == "okx"
    assert attempted == ("binance", "bybit", "okx")
    assert failures == ("binance:blocked_http_451", "bybit:blocked_http_403")


def test_funding_telemetry_is_bounded_when_all_providers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs) -> _Response:
        del url, kwargs
        return _Response(200, {"data": []})

    monkeypatch.setattr(market.httpx, "get", fake_get)

    rate, provider, attempted, failures = market._fetch_funding_rate(
        "cbBTC", 2, ("okx", "unsupported-provider")
    )

    assert rate is None
    assert provider is None
    assert attempted == ("okx", "unsupported-provider")
    assert failures == ("okx:invalid_response", "unsupported-provider:unsupported")

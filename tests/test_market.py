import httpx
import pytest
from types import SimpleNamespace

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


def test_fetch_uses_underlying_quote_but_blocks_wrapper_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs) -> _Response:
        del kwargs
        if url == market.COINGECKO_MARKETS:
            return _Response(403, {})
        if url == market.COINBASE_TICKER.format(pair="BTC-USD"):
            return _Response(200, {"price": "83000"})
        if url == market.COINGECKO_GLOBAL:
            return _Response(200, {"data": {"market_cap_percentage": {"btc": 50}}})
        if url == market.FEAR_GREED_URL:
            return _Response(200, {"data": [{"value": "50"}]})
        raise AssertionError(f"unexpected URL: {url}")

    position = {
        "aave": {"healthFactor": 999, "totalCollateralUSD": 0},
        "reserveRates": {
            "USDC": {"borrowApy": 0.04, "supplyApy": 0.02},
            "cbBTC": {"borrowApy": 0.01, "supplyApy": 0.0},
        },
        "tokenBalances": {},
    }
    onchain = SimpleNamespace(
        available=True,
        usdc_utilization=0.5,
        asset_utilization=0.5,
        recent_liquidations=0,
        asset_frozen=False,
        asset_paused=False,
        borrow_asset_frozen=False,
        borrow_asset_paused=False,
        short_asset_utilization=0.5,
        short_asset_frozen=False,
        short_asset_paused=False,
        risk_available=True,
        reserve_ltv=0.8,
        reserve_liquidation_threshold=0.85,
        reserve_emode_category=None,
        borrow_reserve_ltv=0.8,
        borrow_reserve_liquidation_threshold=0.85,
        borrow_reserve_emode_category=None,
        account_ltv=0.0,
        account_liquidation_threshold=0.0,
        user_emode_category=0,
        emode_liquidation_threshold=None,
        risk_block=1,
        risk_fetched_at="2026-09-29T00:00:00Z",
    )

    monkeypatch.setattr(market.httpx, "get", fake_get)
    monkeypatch.setattr(
        market,
        "_fetch_funding_rate",
        lambda *args, **kwargs: (0.01, "okx", ("okx",), ()),
    )
    monkeypatch.setattr("bot.onchain.fetch", lambda *args, **kwargs: onchain)

    class _MCP:
        wallet_address = "0x" + "0" * 40

        def get_position(self) -> dict:
            return position

    data, failures = market.fetch(
        "cbBTC",
        _MCP(),
        short_borrow_asset="cbBTC",
    )

    assert data.price == 83_000.0
    assert data.price_available is True
    assert data.price_provider == "coinbase_price"
    assert data.price_entry_eligible is False
    assert data.price_protection_eligible is False
    assert "coingecko_prices:blocked_http_403" in failures


def test_fetch_uses_aave_oracle_for_wrapper_entry_when_coingecko_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_get(url: str, **kwargs) -> _Response:
        del kwargs
        if url == market.COINGECKO_MARKETS:
            return _Response(403, {})
        if url == market.COINGECKO_GLOBAL:
            return _Response(200, {"data": {"market_cap_percentage": {"btc": 50}}})
        if url == market.FEAR_GREED_URL:
            return _Response(200, {"data": [{"value": "50"}]})
        raise AssertionError(f"unexpected exchange URL: {url}")

    position = {
        "aave": {"healthFactor": 999, "totalCollateralUSD": 0},
        "reserveRates": {
            "USDC": {"borrowApy": 0.04, "supplyApy": 0.02},
            "cbBTC": {"borrowApy": 0.01, "supplyApy": 0.0},
        },
        "tokenBalances": {},
    }
    onchain = SimpleNamespace(
        available=True,
        asset_price_usd=83_574.67,
        usdc_utilization=0.5,
        asset_utilization=0.5,
        recent_liquidations=0,
        asset_frozen=False,
        asset_paused=False,
        borrow_asset_frozen=False,
        borrow_asset_paused=False,
        short_asset_utilization=0.5,
        short_asset_frozen=False,
        short_asset_paused=False,
        risk_available=True,
        reserve_ltv=0.8,
        reserve_liquidation_threshold=0.85,
        reserve_emode_category=None,
        borrow_reserve_ltv=0.8,
        borrow_reserve_liquidation_threshold=0.85,
        borrow_reserve_emode_category=None,
        account_ltv=0.0,
        account_liquidation_threshold=0.0,
        user_emode_category=0,
        emode_liquidation_threshold=None,
        risk_block=1,
        risk_fetched_at="2026-09-29T00:00:00Z",
    )

    monkeypatch.setattr(market.httpx, "get", fake_get)
    monkeypatch.setattr(
        market,
        "_fetch_funding_rate",
        lambda *args, **kwargs: (0.01, "okx", ("okx",), ()),
    )
    onchain_calls: list[dict] = []

    def fake_onchain_fetch(*args, **kwargs):
        del args
        onchain_calls.append(kwargs)
        return onchain

    monkeypatch.setattr("bot.onchain.fetch", fake_onchain_fetch)

    class _MCP:
        wallet_address = "0x" + "0" * 40

        def get_position(self) -> dict:
            return position

    data, failures = market.fetch(
        "cbBTC",
        _MCP(),
        short_borrow_asset="cbBTC",
    )

    assert data.price == 83_574.67
    assert data.price_provider == "aave_oracle"
    assert data.price_entry_eligible is True
    assert data.price_protection_eligible is True
    assert failures == ["coingecko_prices:blocked_http_403"]
    assert onchain_calls[0]["include_asset_price"] is True


def test_fetch_returns_partial_data_when_all_price_sources_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def blocked_get(url: str, **kwargs) -> _Response:
        del url, kwargs
        return _Response(403, {})

    position = {
        "aave": {"healthFactor": 999, "totalCollateralUSD": 0},
        "reserveRates": {
            "USDC": {"borrowApy": 0.04, "supplyApy": 0.02},
            "cbBTC": {"borrowApy": 0.01, "supplyApy": 0.0},
        },
        "tokenBalances": {},
    }
    onchain = SimpleNamespace(
        available=True,
        usdc_utilization=0.5,
        asset_utilization=0.5,
        recent_liquidations=0,
        asset_frozen=False,
        asset_paused=False,
        borrow_asset_frozen=False,
        borrow_asset_paused=False,
        short_asset_utilization=0.5,
        short_asset_frozen=False,
        short_asset_paused=False,
        risk_available=True,
        reserve_ltv=None,
        reserve_liquidation_threshold=None,
        reserve_emode_category=None,
        borrow_reserve_ltv=None,
        borrow_reserve_liquidation_threshold=None,
        borrow_reserve_emode_category=None,
        account_ltv=None,
        account_liquidation_threshold=None,
        user_emode_category=None,
        emode_liquidation_threshold=None,
        risk_block=None,
        risk_fetched_at=None,
    )

    monkeypatch.setattr(market.httpx, "get", blocked_get)
    monkeypatch.setattr(
        market,
        "_fetch_funding_rate",
        lambda *args, **kwargs: (None, None, ("okx",), ("okx:blocked_http_403",)),
    )
    monkeypatch.setattr("bot.onchain.fetch", lambda *args, **kwargs: onchain)

    class _MCP:
        wallet_address = "0x" + "0" * 40

        def get_position(self) -> dict:
            return position

    data, failures = market.fetch(
        "cbBTC",
        _MCP(),
        short_borrow_asset="cbBTC",
    )

    assert data.price_available is False
    assert data.price == 0.0
    assert data.price_provider is None
    assert any(label.startswith("coingecko_prices:") for label in failures)
    assert all("https://" not in label for label in failures)

"""Market data fetcher with provider-aware degraded-mode telemetry."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
import time
from typing import Optional, Sequence

import httpx

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
COINBASE_TICKER = "https://api.exchange.coinbase.com/products/{pair}/ticker"
KRAKEN_TICKER = "https://api.kraken.com/0/public/Ticker"
BINANCE_PREMIUM = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"
OKX_FUNDING = "https://www.okx.com/api/v5/public/funding-rate"
FEAR_GREED_URL = "https://api.alternative.me/fng/"
FUNDING_PROVIDER_ORDER = ("okx", "binance", "bybit")
SUPPORTED_FUNDING_PROVIDERS = frozenset(FUNDING_PROVIDER_ORDER)

ASSET_TO_CG_ID: dict[str, str] = {
    "WETH": "ethereum",
    "ETH": "ethereum",
    "cbBTC": "coinbase-wrapped-btc",
    "wstETH": "wrapped-steth",
}

ASSET_TO_COINBASE_SPOT: dict[str, str] = {
    "WETH": "ETH-USD",
    "ETH": "ETH-USD",
    "wstETH": "ETH-USD",
    "cbBTC": "BTC-USD",
}
ASSET_TO_KRAKEN_SPOT: dict[str, str] = {
    "WETH": "ETHUSD",
    "ETH": "ETHUSD",
    "wstETH": "ETHUSD",
    "cbBTC": "XBTUSD",
}

# Underlying exchange quotes are useful for emergency reference and trend
# context, but they are not authoritative wrapper quotes for these assets.
_UNDERLYING_PRICE_PROXY_ASSETS = frozenset({"cbbtc", "wsteth"})

# Map bot asset → exchange perpetual symbols for funding rate
ASSET_TO_BINANCE: dict[str, str] = {
    "WETH": "ETHUSDT",
    "ETH": "ETHUSDT",
    "wstETH": "ETHUSDT",
    "cbBTC": "BTCUSDT",
}
ASSET_TO_OKX: dict[str, str] = {
    "WETH": "ETH-USDT-SWAP",
    "ETH": "ETH-USDT-SWAP",
    "wstETH": "ETH-USDT-SWAP",
    "cbBTC": "BTC-USDT-SWAP",
}


@dataclass
class MarketData:
    price: float
    change_1h: float
    change_24h: float
    change_7d: float
    borrow_apr: float  # USDC borrow APR in % (already multiplied by 100)
    btc_dominance: float  # BTC market cap dominance %
    health_factor: float  # current Aave HF (999 = no debt)
    total_collateral_usd: float
    position_data: dict  # raw get_position response
    price_available: bool = True
    price_provider: Optional[str] = None
    price_entry_eligible: bool = True
    price_protection_eligible: bool = True
    price_failures: tuple[str, ...] = ()
    position_available: bool = False  # MCP position snapshot was complete
    onchain_available: bool = False  # all direct Aave safety reads succeeded
    volume_24h: Optional[float] = None  # 24h spot volume in USD (from CoinGecko)
    funding_rate: Optional[float] = (
        None  # perp funding rate in % per 8h (None = unavailable)
    )
    funding_provider: Optional[str] = None
    funding_sources_attempted: tuple[str, ...] = ()
    funding_failures: tuple[str, ...] = ()
    fear_greed: Optional[int] = (
        None  # Crypto Fear & Greed Index 0-100 (None = unavailable)
    )
    usdc_utilization: Optional[float] = None  # Aave v3 Base USDC pool utilization 0–1
    asset_utilization: Optional[float] = (
        None  # Aave v3 Base supply-asset utilization 0–1
    )
    short_asset_utilization: Optional[float] = None
    recent_liquidations: Optional[int] = None  # LiquidationCall events in last ~5 min
    # Reserve status flags — None = RPC unavailable (treated as safe/unknown)
    asset_frozen: Optional[bool] = None  # supply asset frozen (flash loans blocked)
    asset_paused: Optional[bool] = None  # supply asset paused (all ops blocked)
    borrow_asset_frozen: Optional[bool] = None  # USDC frozen (long flash loan blocked)
    borrow_asset_paused: Optional[bool] = None  # USDC paused (all ops blocked)
    short_asset_frozen: Optional[bool] = None
    short_asset_paused: Optional[bool] = None
    risk_data_available: bool = False
    reserve_ltv: Optional[float] = None
    reserve_liquidation_threshold: Optional[float] = None
    reserve_emode_category: Optional[int] = None
    borrow_reserve_ltv: Optional[float] = None
    borrow_reserve_liquidation_threshold: Optional[float] = None
    borrow_reserve_emode_category: Optional[int] = None
    account_ltv: Optional[float] = None
    account_liquidation_threshold: Optional[float] = None
    user_emode_category: Optional[int] = None
    emode_liquidation_threshold: Optional[float] = None
    risk_block: Optional[int] = None
    risk_fetched_at: Optional[str] = None
    usdc_supply_apy: Optional[float] = (
        None  # Aave USDC supply APY % (earned on short collateral)
    )
    asset_borrow_apy: Optional[float] = (
        None  # Aave asset borrow APY % (paid when short)
    )
    short_borrow_apr: Optional[float] = None  # borrow APR for short_borrow_asset
    wallet_collateral_usd: float = 0.0  # wallet balance in USD (USDC + asset×price) — used when Aave collateral is 0
    # Local observation metadata. These timestamps identify when this process
    # completed each read; they are not claims about provider event time.
    source_observed_at: dict[str, str] = field(default_factory=dict)
    source_fetch_duration_ms: dict[str, float] = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_source(
    observed_at: dict[str, str],
    durations_ms: dict[str, float],
    source: str,
    started: float,
) -> None:
    observed_at[source] = _now_iso()
    durations_ms[source] = round(max(time.monotonic() - started, 0.0) * 1000, 1)


def _source_failure_label(source: str, error: Exception) -> str:
    """Return bounded provider telemetry without URLs or response bodies."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in (401, 403, 451):
        return f"{source}:blocked_http_{status_code}"
    if status_code is not None:
        return f"{source}:http_{status_code}"
    if isinstance(error, httpx.TimeoutException):
        return f"{source}:timeout"
    if isinstance(error, (KeyError, IndexError, TypeError, ValueError)):
        return f"{source}:invalid_response"
    return f"{source}:request_error"


def _valid_price(value: object) -> float:
    """Return a positive finite price or raise for an invalid provider value."""
    price = float(value)
    if not isfinite(price) or price <= 0:
        raise ValueError("price is not positive and finite")
    return price


def _fetch_reference_price(
    asset: str,
    timeout: int,
    observed_at: dict[str, str],
    durations_ms: dict[str, float],
) -> tuple[Optional[float], Optional[str], tuple[str, ...]]:
    """Fetch a current exchange quote after the wrapper-aware source fails.

    These quotes are explicitly marked as reference proxies by the caller for
    wrapper assets such as cbBTC and wstETH. They are never silently promoted
    to authoritative entry prices.
    """
    failures: list[str] = []
    providers = (
        ("coinbase_price", ASSET_TO_COINBASE_SPOT.get(asset)),
        ("kraken_price", ASSET_TO_KRAKEN_SPOT.get(asset)),
    )
    for provider, pair in providers:
        started = time.monotonic()
        try:
            if not pair:
                raise ValueError("unsupported asset")
            if provider == "coinbase_price":
                response = httpx.get(COINBASE_TICKER.format(pair=pair), timeout=timeout)
                response.raise_for_status()
                price = _valid_price(response.json()["price"])
            else:
                response = httpx.get(
                    KRAKEN_TICKER,
                    params={"pair": pair},
                    timeout=timeout,
                )
                response.raise_for_status()
                result = response.json().get("result", {})
                ticker = next(
                    (value for key, value in result.items() if key != "last"), None
                )
                price = _valid_price(ticker["c"][0])
        except Exception as error:
            failures.append(_source_failure_label(provider, error))
        else:
            return price, provider, tuple(failures)
        finally:
            _record_source(observed_at, durations_ms, provider, started)
    return None, None, tuple(failures)


def _funding_response(provider: str, asset: str, timeout: int):
    """Fetch one provider's funding response for ``asset``."""
    if provider == "okx":
        symbol = ASSET_TO_OKX.get(asset, "BTC-USDT-SWAP")
        return httpx.get(
            OKX_FUNDING,
            params={"instId": symbol},
            timeout=timeout,
        )
    if provider == "binance":
        symbol = ASSET_TO_BINANCE.get(asset, "BTCUSDT")
        return httpx.get(
            BINANCE_PREMIUM,
            params={"symbol": symbol},
            timeout=timeout,
        )
    if provider == "bybit":
        symbol = ASSET_TO_BINANCE.get(asset, "BTCUSDT")
        return httpx.get(
            BYBIT_TICKERS,
            params={"category": "linear", "symbol": symbol},
            timeout=timeout,
        )
    raise ValueError(f"unsupported funding provider: {provider}")


def _parse_funding_rate(provider: str, payload: dict) -> float:
    """Return a provider funding rate as a percentage per 8h."""
    if provider == "okx":
        raw_rate = payload["data"][0]["fundingRate"]
    elif provider == "binance":
        raw_rate = payload["lastFundingRate"]
    elif provider == "bybit":
        raw_rate = payload["result"]["list"][0]["fundingRate"]
    else:
        raise ValueError(f"unsupported funding provider: {provider}")

    rate = float(raw_rate) * 100
    if not isfinite(rate):
        raise ValueError("funding rate is not finite")
    return rate


def _funding_failure_label(provider: str, error: Exception) -> str:
    """Return bounded, non-sensitive telemetry for one failed provider."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in (403, 451):
        return f"{provider}:blocked_http_{status_code}"
    if status_code is not None:
        return f"{provider}:http_{status_code}"
    if isinstance(error, httpx.TimeoutException):
        return f"{provider}:timeout"
    if isinstance(error, (KeyError, IndexError, TypeError, ValueError)):
        return f"{provider}:invalid_response"
    return f"{provider}:request_error"


def _fetch_funding_rate(
    asset: str,
    timeout: int,
    providers: Optional[Sequence[str]] = None,
) -> tuple[Optional[float], Optional[str], tuple[str, ...], tuple[str, ...]]:
    """Try funding providers in order and return rate plus bounded telemetry."""
    provider_order = (
        tuple(providers) if providers is not None else FUNDING_PROVIDER_ORDER
    )
    attempted: list[str] = []
    failures: list[str] = []

    for configured_provider in provider_order:
        provider = str(configured_provider).strip().lower()
        attempted.append(provider)
        if provider not in SUPPORTED_FUNDING_PROVIDERS:
            failures.append(f"{provider}:unsupported")
            continue
        try:
            response = _funding_response(provider, asset, timeout)
            response.raise_for_status()
            rate = _parse_funding_rate(provider, response.json())
        except Exception as error:
            failures.append(_funding_failure_label(provider, error))
            continue
        return rate, provider, tuple(attempted), tuple(failures)

    return None, None, tuple(attempted), tuple(failures)


def fetch(
    asset: str,
    mcp_client,
    timeout: int = 15,
    rpc_url: str = "https://mainnet.base.org",
    onchain_lookback_blocks: int = 150,
    short_borrow_asset: Optional[str] = None,
    funding_sources: Optional[Sequence[str]] = None,
) -> tuple[MarketData, list[str]]:
    """
    Fetch market and safety data and return (MarketData, sources_failed).

    Partial data is returned instead of raising for provider degradation. The
    caller decides whether the current scope is safe to continue: flat/new
    exposure must fail closed, while an open position can still use direct
    Aave risk data for defensive actions.
    """
    sources_failed: list[str] = []
    source_observed_at: dict[str, str] = {}
    source_fetch_duration_ms: dict[str, float] = {}

    # ── Source 1: CoinGecko wrapper-aware price/trend data ────────────────
    price = change_1h = change_24h = change_7d = volume_24h = None
    price_provider: Optional[str] = None
    price_entry_eligible = False
    price_protection_eligible = False
    price_failures: list[str] = []
    source_started = time.monotonic()
    try:
        cg_id = ASSET_TO_CG_ID.get(asset, asset.lower())
        r = httpx.get(
            COINGECKO_MARKETS,
            params={
                "vs_currency": "usd",
                "ids": cg_id,
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=timeout,
        )
        r.raise_for_status()
        coin = r.json()[0]
        price = _valid_price(coin["current_price"])
        change_1h = float(coin.get("price_change_percentage_1h_in_currency") or 0)
        change_24h = float(coin.get("price_change_percentage_24h_in_currency") or 0)
        change_7d = float(coin.get("price_change_percentage_7d_in_currency") or 0)
        volume_24h = float(coin.get("total_volume") or 0) or None
        price_provider = "coingecko"
        price_entry_eligible = True
        price_protection_eligible = True
    except Exception as e:
        failure = _source_failure_label("coingecko_prices", e)
        sources_failed.append(failure)
        price_failures.append(failure)
    finally:
        _record_source(
            source_observed_at,
            source_fetch_duration_ms,
            "coingecko_prices",
            source_started,
        )

    # CoinGecko can be blocked independently of other public APIs. Keep a
    # current exchange quote available for defensive reference, but do not use
    # an underlying BTC/ETH quote to open a wrapper asset position.
    if price is None:
        fallback_price, fallback_provider, fallback_failures = _fetch_reference_price(
            asset,
            timeout,
            source_observed_at,
            source_fetch_duration_ms,
        )
        sources_failed.extend(fallback_failures)
        price_failures.extend(fallback_failures)
        if fallback_price is not None:
            price = fallback_price
            price_provider = fallback_provider
            is_proxy = asset.casefold() in _UNDERLYING_PRICE_PROXY_ASSETS
            price_entry_eligible = not is_proxy
            price_protection_eligible = not is_proxy

    # ── Source 2: get_position (on-chain Aave state) ──────────────────────
    pos: Optional[dict] = None
    position_available = False
    short_asset = short_borrow_asset or asset
    borrow_apr = health_factor = total_collateral_usd = None
    short_borrow_apr = None
    source_started = time.monotonic()
    try:
        pos = mcp_client.get_position()
        rates = pos.get("reserveRates", {})
        borrow_apr = rates["USDC"]["borrowApy"] * 100
        short_borrow_apr = rates[short_asset]["borrowApy"] * 100
        health_factor = float(pos["aave"]["healthFactor"])
        total_collateral_usd = float(pos["aave"]["totalCollateralUSD"])
        position_available = isinstance(pos, dict) and isinstance(pos.get("aave"), dict)
    except Exception as e:
        sources_failed.append(_source_failure_label("get_position", e))
    finally:
        _record_source(
            source_observed_at,
            source_fetch_duration_ms,
            "get_position",
            source_started,
        )
    if not position_available:
        sources_failed.append("get_position:incomplete_snapshot")

    # Wallet balances — used as collateral fallback when Aave position is closed
    # wallet_balances keys are token amounts (not USD); USDC ≈ $1 so it's direct.
    # For the supply asset (e.g. WETH, cbBTC), multiply by price (populated below if CG succeeded).
    wallet_collateral_usd = 0.0
    if pos is not None:
        # MCP returns balances under "tokenBalances"; fall back to "wallet_balances" for compatibility
        wb = pos.get("tokenBalances") or pos.get("wallet_balances") or {}
        try:
            wallet_usdc = float(wb.get("USDC", 0) or 0)
            wallet_collateral_usd = wallet_usdc
        except (TypeError, ValueError):
            pass

    # Extract short carry rates from position data (both optional — keys may not exist)
    usdc_supply_apy = asset_borrow_apy = None
    if pos is not None:
        try:
            rates = pos.get("reserveRates", {})
            usdc_supply_apy = round(rates["USDC"]["supplyApy"] * 100, 4)
            asset_borrow_apy = round(rates[short_asset]["borrowApy"] * 100, 4)
        except (KeyError, TypeError):
            pass

    # ── Source 3: CoinGecko global (BTC dominance) ────────────────────────
    btc_dominance = None
    source_started = time.monotonic()
    try:
        r = httpx.get(COINGECKO_GLOBAL, timeout=timeout)
        r.raise_for_status()
        btc_dominance = float(r.json()["data"]["market_cap_percentage"]["btc"])
    except Exception as e:
        sources_failed.append(_source_failure_label("coingecko_global", e))
    finally:
        _record_source(
            source_observed_at,
            source_fetch_duration_ms,
            "coingecko_global",
            source_started,
        )

    # ── Source 4: Funding rate — prefer the globally reachable provider ────
    source_started = time.monotonic()
    try:
        funding_rate, funding_provider, funding_sources_attempted, funding_failures = (
            _fetch_funding_rate(asset, timeout, funding_sources)
        )
        if funding_rate is None:
            sources_failed.append(
                "funding_rate:" + "; ".join(funding_failures or ("no_provider",))
            )
    finally:
        _record_source(
            source_observed_at,
            source_fetch_duration_ms,
            "funding_rate",
            source_started,
        )

    # ── Source 5: On-chain Aave v3 Base state (soft — failure logged, not blocking) ──
    from bot.onchain import fetch as onchain_fetch

    source_started = time.monotonic()
    try:
        oc = onchain_fetch(
            asset,
            rpc_url,
            onchain_lookback_blocks,
            borrow_asset="USDC",
            short_asset=short_borrow_asset,
            user_address=getattr(mcp_client, "wallet_address", None),
        )
    finally:
        _record_source(
            source_observed_at,
            source_fetch_duration_ms,
            "onchain",
            source_started,
        )
    if not oc.available:
        sources_failed.append("onchain:all_fields_unavailable")

    # ── Source 6: Fear & Greed Index (soft — failure logged, not blocking) ────
    fear_greed = None
    source_started = time.monotonic()
    try:
        r = httpx.get(FEAR_GREED_URL, params={"limit": 1}, timeout=timeout)
        r.raise_for_status()
        fear_greed = int(r.json()["data"][0]["value"])
    except Exception as e:
        sources_failed.append(_source_failure_label("fear_greed", e))
    finally:
        _record_source(
            source_observed_at,
            source_fetch_duration_ms,
            "fear_greed",
            source_started,
        )

    # Add asset wallet balance in USD now that we have price — sum both tokens
    # so mixed wallets (e.g. USDC + cbBTC) size correctly. _ensure_wallet_token
    # will swap whatever is needed into the right token before opening.
    if pos is not None and price is not None and price > 0:
        try:
            wb = pos.get("tokenBalances") or pos.get("wallet_balances") or {}
            wallet_asset_usd = float(wb.get(asset, 0) or 0) * price
            wallet_collateral_usd += wallet_asset_usd
        except (TypeError, ValueError):
            pass

    return MarketData(
        price=price or 0.0,
        change_1h=change_1h or 0.0,
        change_24h=change_24h or 0.0,
        change_7d=change_7d or 0.0,
        borrow_apr=borrow_apr or 0.0,
        btc_dominance=btc_dominance or 0.0,
        health_factor=health_factor if health_factor is not None else 999.0,
        total_collateral_usd=total_collateral_usd or 0.0,
        position_data=pos or {},
        price_available=price is not None,
        price_provider=price_provider,
        price_entry_eligible=price_entry_eligible,
        price_protection_eligible=price_protection_eligible,
        price_failures=tuple(price_failures),
        position_available=position_available,
        onchain_available=oc.available,
        volume_24h=volume_24h,
        funding_rate=funding_rate,
        funding_provider=funding_provider,
        funding_sources_attempted=funding_sources_attempted,
        funding_failures=funding_failures,
        fear_greed=fear_greed,
        usdc_utilization=oc.usdc_utilization,
        asset_utilization=oc.asset_utilization,
        short_asset_utilization=oc.short_asset_utilization,
        recent_liquidations=oc.recent_liquidations,
        asset_frozen=oc.asset_frozen,
        asset_paused=oc.asset_paused,
        borrow_asset_frozen=oc.borrow_asset_frozen,
        borrow_asset_paused=oc.borrow_asset_paused,
        short_asset_frozen=oc.short_asset_frozen,
        short_asset_paused=oc.short_asset_paused,
        risk_data_available=oc.risk_available,
        reserve_ltv=oc.reserve_ltv,
        reserve_liquidation_threshold=oc.reserve_liquidation_threshold,
        reserve_emode_category=oc.reserve_emode_category,
        borrow_reserve_ltv=oc.borrow_reserve_ltv,
        borrow_reserve_liquidation_threshold=oc.borrow_reserve_liquidation_threshold,
        borrow_reserve_emode_category=oc.borrow_reserve_emode_category,
        account_ltv=oc.account_ltv,
        account_liquidation_threshold=oc.account_liquidation_threshold,
        user_emode_category=oc.user_emode_category,
        emode_liquidation_threshold=oc.emode_liquidation_threshold,
        risk_block=oc.risk_block,
        risk_fetched_at=oc.risk_fetched_at,
        usdc_supply_apy=usdc_supply_apy,
        asset_borrow_apy=asset_borrow_apy,
        short_borrow_apr=short_borrow_apr,
        wallet_collateral_usd=wallet_collateral_usd,
        source_observed_at=source_observed_at,
        source_fetch_duration_ms=source_fetch_duration_ms,
    ), sources_failed

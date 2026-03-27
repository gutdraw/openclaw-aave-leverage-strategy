"""
Market data fetcher — pulls from 3 independent sources.
Requires at least 2 to succeed, otherwise raises RuntimeError("insufficient_data:...").
"""
from dataclasses import dataclass
from typing import Optional

import httpx

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_GLOBAL  = "https://api.coingecko.com/api/v3/global"
BINANCE_PREMIUM   = "https://fapi.binance.com/fapi/v1/premiumIndex"
BYBIT_TICKERS     = "https://api.bybit.com/v5/market/tickers"
OKX_FUNDING       = "https://www.okx.com/api/v5/public/funding-rate"
FEAR_GREED_URL    = "https://api.alternative.me/fng/"

ASSET_TO_CG_ID: dict[str, str] = {
    "WETH":   "ethereum",
    "ETH":    "ethereum",
    "cbBTC":  "coinbase-wrapped-btc",
    "wstETH": "wrapped-steth",
}

# Map bot asset → exchange perpetual symbols for funding rate
ASSET_TO_BINANCE: dict[str, str] = {
    "WETH":   "ETHUSDT",
    "ETH":    "ETHUSDT",
    "wstETH": "ETHUSDT",
    "cbBTC":  "BTCUSDT",
}
ASSET_TO_OKX: dict[str, str] = {
    "WETH":   "ETH-USDT-SWAP",
    "ETH":    "ETH-USDT-SWAP",
    "wstETH": "ETH-USDT-SWAP",
    "cbBTC":  "BTC-USDT-SWAP",
}


@dataclass
class MarketData:
    price: float
    change_1h: float
    change_24h: float
    change_7d: float
    borrow_apr: float             # USDC borrow APR in % (already multiplied by 100)
    btc_dominance: float          # BTC market cap dominance %
    health_factor: float          # current Aave HF (999 = no debt)
    total_collateral_usd: float
    position_data: dict           # raw get_position response
    volume_24h: Optional[float] = None        # 24h spot volume in USD (from CoinGecko)
    funding_rate: Optional[float] = None      # perp funding rate in % per 8h (None = unavailable)
    fear_greed: Optional[int] = None          # Crypto Fear & Greed Index 0-100 (None = unavailable)
    usdc_utilization: Optional[float] = None  # Aave v3 Base USDC pool utilization 0–1
    asset_utilization: Optional[float] = None # Aave v3 Base supply-asset utilization 0–1
    recent_liquidations: Optional[int] = None # LiquidationCall events in last ~5 min
    usdc_supply_apy: Optional[float] = None   # Aave USDC supply APY % (earned on short collateral)
    asset_borrow_apy: Optional[float] = None  # Aave asset borrow APY % (paid when short)


def fetch(
    asset: str,
    mcp_client,
    timeout: int = 15,
    rpc_url: str = "https://mainnet.base.org",
    onchain_lookback_blocks: int = 10,
) -> tuple[MarketData, list[str]]:
    """
    Fetch from all 3 sources and return (MarketData, sources_failed).
    Raises RuntimeError if fewer than 2 sources succeed.
    """
    sources_failed: list[str] = []

    # ── Source 1: CoinGecko coin prices ───────────────────────────────────
    price = change_1h = change_24h = change_7d = volume_24h = None
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
        coin       = r.json()[0]
        price      = float(coin["current_price"])
        change_1h  = float(coin.get("price_change_percentage_1h_in_currency") or 0)
        change_24h = float(coin.get("price_change_percentage_24h_in_currency") or 0)
        change_7d  = float(coin.get("price_change_percentage_7d_in_currency") or 0)
        volume_24h = float(coin.get("total_volume") or 0) or None
    except Exception as e:
        sources_failed.append(f"coingecko_prices:{e}")

    # ── Source 2: get_position (on-chain Aave state) ──────────────────────
    pos: Optional[dict] = None
    borrow_apr = health_factor = total_collateral_usd = None
    try:
        pos                  = mcp_client.get_position()
        rates                = pos.get("reserveRates", {})
        borrow_apr           = rates["USDC"]["borrowApy"] * 100
        health_factor        = float(pos["aave"]["healthFactor"])
        total_collateral_usd = float(pos["aave"]["totalCollateralUSD"])
    except Exception as e:
        sources_failed.append(f"get_position:{e}")

    # Extract short carry rates from position data (both optional — keys may not exist)
    usdc_supply_apy = asset_borrow_apy = None
    if pos is not None:
        try:
            rates = pos.get("reserveRates", {})
            usdc_supply_apy  = round(rates["USDC"]["supplyApy"] * 100, 4)
            asset_borrow_apy = round(rates[asset]["borrowApy"] * 100, 4)
        except (KeyError, TypeError):
            pass

    # ── Source 3: CoinGecko global (BTC dominance) ────────────────────────
    btc_dominance = None
    try:
        r = httpx.get(COINGECKO_GLOBAL, timeout=timeout)
        r.raise_for_status()
        btc_dominance = float(r.json()["data"]["market_cap_percentage"]["btc"])
    except Exception as e:
        sources_failed.append(f"coingecko_global:{e}")

    # ── Source 4: Funding rate — tries Binance → Bybit → OKX in order ───────
    # Binance/Bybit block US IPs (451/403); OKX is accessible globally.
    funding_rate = None
    _fr_errors: list[str] = []
    try:
        symbol = ASSET_TO_BINANCE.get(asset, "BTCUSDT")
        r = httpx.get(BINANCE_PREMIUM, params={"symbol": symbol}, timeout=timeout)
        r.raise_for_status()
        funding_rate = float(r.json()["lastFundingRate"]) * 100
    except Exception as e:
        _fr_errors.append(f"binance:{e}")
    if funding_rate is None:
        try:
            symbol = ASSET_TO_BINANCE.get(asset, "BTCUSDT")
            r = httpx.get(BYBIT_TICKERS, params={"category": "linear", "symbol": symbol}, timeout=timeout)
            r.raise_for_status()
            funding_rate = float(r.json()["result"]["list"][0]["fundingRate"]) * 100
        except Exception as e:
            _fr_errors.append(f"bybit:{e}")
    if funding_rate is None:
        try:
            okx_id = ASSET_TO_OKX.get(asset, "BTC-USDT-SWAP")
            r = httpx.get(OKX_FUNDING, params={"instId": okx_id}, timeout=timeout)
            r.raise_for_status()
            funding_rate = float(r.json()["data"][0]["fundingRate"]) * 100
        except Exception as e:
            _fr_errors.append(f"okx:{e}")
            sources_failed.append(f"funding_rate:{'; '.join(_fr_errors)}")

    # ── Source 5: On-chain Aave v3 Base state (soft — failure logged, not blocking) ──
    from bot.onchain import fetch as onchain_fetch
    oc = onchain_fetch(asset, rpc_url, onchain_lookback_blocks)
    if oc.usdc_utilization is None and oc.recent_liquidations is None:
        sources_failed.append("onchain:all_fields_unavailable")

    # ── Source 6: Fear & Greed Index (soft — failure logged, not blocking) ────
    fear_greed = None
    try:
        r = httpx.get(FEAR_GREED_URL, params={"limit": 1}, timeout=timeout)
        r.raise_for_status()
        fear_greed = int(r.json()["data"][0]["value"])
    except Exception as e:
        sources_failed.append(f"fear_greed:{e}")

    succeeded = sum(x is not None for x in [price, borrow_apr, btc_dominance])
    if succeeded < 2:
        raise RuntimeError(
            f"insufficient_data: {succeeded}/3 sources succeeded. "
            f"Failures: {sources_failed}"
        )

    # Price is mandatory — without it we cannot size positions or compute P&L
    if price is None:
        raise RuntimeError(
            f"insufficient_data: price unavailable (CoinGecko failed). "
            f"Failures: {sources_failed}"
        )

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
        volume_24h=volume_24h,
        funding_rate=funding_rate,
        fear_greed=fear_greed,
        usdc_utilization=oc.usdc_utilization,
        asset_utilization=oc.asset_utilization,
        recent_liquidations=oc.recent_liquidations,
        usdc_supply_apy=usdc_supply_apy,
        asset_borrow_apy=asset_borrow_apy,
    ), sources_failed

"""
Market data fetcher — pulls from 3 independent sources.
Requires at least 2 to succeed, otherwise raises RuntimeError("insufficient_data:...").
"""
from dataclasses import dataclass
from typing import Optional

import httpx

COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_GLOBAL  = "https://api.coingecko.com/api/v3/global"

ASSET_TO_CG_ID: dict[str, str] = {
    "WETH":   "ethereum",
    "ETH":    "ethereum",
    "cbBTC":  "coinbase-wrapped-btc",
    "wstETH": "wrapped-steth",
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


def fetch(
    asset: str,
    mcp_client,
    timeout: int = 15,
) -> tuple[MarketData, list[str]]:
    """
    Fetch from all 3 sources and return (MarketData, sources_failed).
    Raises RuntimeError if fewer than 2 sources succeed.
    """
    sources_failed: list[str] = []

    # ── Source 1: CoinGecko coin prices ───────────────────────────────────
    price = change_1h = change_24h = change_7d = None
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
    except Exception as e:
        sources_failed.append(f"coingecko_prices:{e}")

    # ── Source 2: get_position (on-chain Aave state) ──────────────────────
    pos: Optional[dict] = None
    borrow_apr = health_factor = total_collateral_usd = None
    try:
        pos                  = mcp_client.get_position()
        borrow_apr           = pos["reserveRates"]["USDC"]["borrowApy"] * 100
        health_factor        = float(pos["aave"]["healthFactor"])
        total_collateral_usd = float(pos["aave"]["totalCollateralUSD"])
    except Exception as e:
        sources_failed.append(f"get_position:{e}")

    # ── Source 3: CoinGecko global (BTC dominance) ────────────────────────
    btc_dominance = None
    try:
        r = httpx.get(COINGECKO_GLOBAL, timeout=timeout)
        r.raise_for_status()
        btc_dominance = float(r.json()["data"]["market_cap_percentage"]["btc"])
    except Exception as e:
        sources_failed.append(f"coingecko_global:{e}")

    succeeded = sum(x is not None for x in [price, borrow_apr, btc_dominance])
    if succeeded < 2:
        raise RuntimeError(
            f"insufficient_data: {succeeded}/3 sources succeeded. "
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
    ), sources_failed

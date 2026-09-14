"""
On-chain data fetcher — reads Aave v3 pool state directly from Base.

Three signals, all read-only (eth_call / eth_getLogs), free public RPC:

  1. USDC utilization  — varDebtToken.totalSupply() / aToken.totalSupply()
     Aave's interest rate curve has a sharp kink at the optimal utilization
     point (~90%). Above the kink borrow APR climbs steeply. At 92% the
     variable rate is already well above typical — a lagging indicator in
     the MCP borrow_apr is too slow to catch this in real-time.

  2. Recent liquidations — LiquidationCall events in the last ~5 minutes.
     A spike in liquidations means the market is under stress. Opening a
     new leveraged position into a cascade amplifies risk.

  3. Reserve flags — isFrozen / isPaused bits from getReserveData().
     Frozen = no new supply/borrow (flash loans blocked); existing positions
     can still repay/withdraw.
     Paused = ALL operations blocked, including repay and withdraw.
     Both states mean we cannot close via flash loan and should exit
     while we still can.

  All fields are Optional so callers can log partial results, but ``available``
  is false unless every safety read succeeds. Live callers must fail closed on
  an unavailable safety snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from web3 import Web3

log = logging.getLogger(__name__)

# ── Aave v3 Base contract addresses (verified from wallet_reader.py) ──────────
AAVE_POOL_BASE = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"

# Underlying asset addresses on Base mainnet
_ASSET_ADDR: dict[str, str] = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "wstETH": "0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452",
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

# aToken and variableDebtToken addresses per asset
_ATOKEN: dict[str, str] = {
    "WETH": "0xD4a0e0b9149BCee3C920d2E00b5dE09138fd8bb7",
    "wstETH": "0x99CBC45ea5bb7EF3a5BC08FB1B7E56Bb2442Ef0D",
    "cbBTC": "0xBdB9300b7CDE636d9CD4AFF00f6F009fFBBc8eE6",
    "USDC": "0x4e65fE4DbA92790696d040ac24Aa414708F5c0AB",
}
_VARDEBT: dict[str, str] = {
    "WETH": "0x24e6e0795b3c7c71D965fCc4f371803d1c1DcA1E",
    "wstETH": "0x41A7C3f5904ad176dACbb1D99101F59ef0811DC1",
    "cbBTC": "0x05E08702028De6AAD395Dc6478B554a56920B9AD",
    "USDC": "0x59dca05b6c26dbd64b5381374aaac5cd05644c28",
}


def canonical_asset(value: object) -> str:
    """Normalize a configured symbol or known Base asset address."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    lowered = raw.casefold()
    for symbol, address in _ASSET_ADDR.items():
        if lowered in {symbol.casefold(), address.casefold()}:
            return symbol.casefold()
    return lowered


def asset_address(symbol: str) -> Optional[str]:
    """Return a known Base underlying address using case-insensitive symbols."""
    wanted = str(symbol or "").casefold()
    for name, address in _ASSET_ADDR.items():
        if name.casefold() == wanted or address.casefold() == wanted:
            return address
    return None


_TOTAL_SUPPLY_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

_POOL_RISK_ABI = [
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"name": "totalCollateralBase", "type": "uint256"},
            {"name": "totalDebtBase", "type": "uint256"},
            {"name": "availableBorrowsBase", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserEMode",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "id", "type": "uint8"}],
        "name": "getEModeCategoryData",
        "outputs": [
            {"name": "ltv", "type": "uint16"},
            {"name": "liquidationThreshold", "type": "uint16"},
            {"name": "liquidationBonus", "type": "uint16"},
            {"name": "priceSource", "type": "address"},
            {"name": "label", "type": "string"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# Default lookback for eth_getLogs.
# Alchemy free tier: max 10 blocks. PAYG: up to 2000.
# Base produces ~1 block/2s → 10 blocks ≈ 20 seconds, 150 blocks ≈ 5 minutes.
_LOOKBACK_BLOCKS_DEFAULT = 10


@dataclass
class OnChainData:
    usdc_utilization: Optional[float]  # 0.0–1.0  e.g. 0.88 = 88% utilised
    asset_utilization: Optional[float]  # supply-side utilization of the trade asset
    recent_liquidations: Optional[int]  # LiquidationCall count in last ~5 min
    available: bool = False
    # Reserve status flags — None means RPC call failed (treated as safe/unknown)
    asset_frozen: Optional[bool] = None  # supply asset frozen (no new borrows/deposits)
    asset_paused: Optional[bool] = None  # supply asset paused (all ops blocked)
    borrow_asset_frozen: Optional[bool] = None  # borrow asset (USDC) frozen
    borrow_asset_paused: Optional[bool] = None  # borrow asset (USDC) paused
    short_asset_utilization: Optional[float] = None
    short_asset_frozen: Optional[bool] = None
    short_asset_paused: Optional[bool] = None
    # Dynamic reserve/account risk data. Values are basis-point fields converted
    # to ratios, so callers do not need protocol-specific bit decoding.
    risk_available: bool = False
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


@dataclass(frozen=True)
class AccountRiskData:
    """Direct Aave account-risk probe result."""

    available: bool
    health_factor: Optional[float] = None
    total_collateral_usd: Optional[float] = None
    total_debt_usd: Optional[float] = None
    block_number: Optional[int] = None
    fetched_at: Optional[str] = None
    error: Optional[str] = None


def fetch_account_risk(
    rpc_url: str,
    user_address: Optional[str],
    timeout: float = 10,
) -> AccountRiskData:
    """Read the Aave account health factor with view calls only.

    This intentionally uses a separate, minimal path from the trading cycle:
    it creates no signer, does not use MCP, and never broadcasts a transaction.
    Errors are reduced to exception type names so callers can persist the result
    without copying RPC URLs or provider response details into runtime files.
    """
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not user_address or not Web3.is_address(user_address):
        return AccountRiskData(
            available=False,
            fetched_at=fetched_at,
            error="invalid_user_address",
        )

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(AAVE_POOL_BASE), abi=_POOL_RISK_ABI
        )
        account = pool.functions.getUserAccountData(
            Web3.to_checksum_address(user_address)
        ).call()
        if not isinstance(account, (list, tuple)) or len(account) < 6:
            raise ValueError("invalid account risk response")

        raw_health_factor = int(account[5])
        # Aave returns uint256 max when there is no debt. Keep the existing
        # bot convention of 999 for an effectively unbounded health factor.
        health_factor = (
            999.0 if raw_health_factor >= 10**30 else raw_health_factor / 10**18
        )
        try:
            block_number = int(w3.eth.block_number)
        except Exception:
            block_number = None
        return AccountRiskData(
            available=True,
            health_factor=health_factor,
            # Aave's base currency values use 8 decimals and represent USD.
            total_collateral_usd=float(account[0]) / 10**8,
            total_debt_usd=float(account[1]) / 10**8,
            block_number=block_number,
            fetched_at=fetched_at,
        )
    except Exception as error:
        log.debug("account risk probe error: %s", type(error).__name__)
        return AccountRiskData(
            available=False,
            fetched_at=fetched_at,
            error=type(error).__name__,
        )


def fetch(
    asset: str,
    rpc_url: str = "https://mainnet.base.org",
    lookback_blocks: int = _LOOKBACK_BLOCKS_DEFAULT,
    borrow_asset: str = "USDC",
    short_asset: Optional[str] = None,
    user_address: Optional[str] = None,
) -> OnChainData:
    """
    Fetch on-chain Aave v3 state from Base. Never raises — returns unavailable
    data on error so live callers can fail closed.
    A single Web3 connection is created per call (read-only, no wallet needed).
    """
    usdc_util = asset_util = recent_liq = None
    asset_frozen = asset_paused = borrow_frozen = borrow_paused = None
    short_asset_util = short_asset_frozen = short_asset_paused = None
    risk: dict = {}
    borrow_risk: dict = {}
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))

        usdc_util = _utilization(w3, "USDC")
        asset_util = _utilization(w3, asset)
        recent_liq = _recent_liquidations(w3, lookback_blocks)

        # Reserve status flags — check both the supply asset and the borrow asset.
        # Frozen = no new supply/borrow (flash loans blocked).
        # Paused = all operations blocked including repay/withdraw.
        asset_frozen, asset_paused = _reserve_flags(w3, asset)
        borrow_frozen, borrow_paused = _reserve_flags(w3, borrow_asset)
        short_asset_name = short_asset or asset
        short_asset_util = _utilization(w3, short_asset_name)
        short_asset_frozen, short_asset_paused = _reserve_flags(w3, short_asset_name)
        risk = _risk_snapshot(w3, asset, user_address)
        borrow_risk = _risk_snapshot(w3, borrow_asset, None)
        risk["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    except Exception as e:
        log.debug("onchain.fetch error: %s", e)

    available = all(
        value is not None
        for value in (
            usdc_util,
            asset_util,
            recent_liq,
            asset_frozen,
            asset_paused,
            borrow_frozen,
            borrow_paused,
            short_asset_util,
            short_asset_frozen,
            short_asset_paused,
        )
    )

    return OnChainData(
        available=available,
        usdc_utilization=usdc_util,
        asset_utilization=asset_util,
        recent_liquidations=recent_liq,
        asset_frozen=asset_frozen,
        asset_paused=asset_paused,
        borrow_asset_frozen=borrow_frozen,
        borrow_asset_paused=borrow_paused,
        short_asset_utilization=short_asset_util,
        short_asset_frozen=short_asset_frozen,
        short_asset_paused=short_asset_paused,
        risk_available=bool(risk.get("available") and borrow_risk.get("available")),
        reserve_ltv=risk.get("reserve_ltv"),
        reserve_liquidation_threshold=risk.get("reserve_liquidation_threshold"),
        reserve_emode_category=risk.get("reserve_emode_category"),
        borrow_reserve_ltv=borrow_risk.get("reserve_ltv"),
        borrow_reserve_liquidation_threshold=borrow_risk.get(
            "reserve_liquidation_threshold"
        ),
        borrow_reserve_emode_category=borrow_risk.get("reserve_emode_category"),
        account_ltv=risk.get("account_ltv"),
        account_liquidation_threshold=risk.get("account_liquidation_threshold"),
        user_emode_category=risk.get("user_emode_category"),
        emode_liquidation_threshold=risk.get("emode_liquidation_threshold"),
        risk_block=risk.get("risk_block"),
        risk_fetched_at=risk.get("fetched_at"),
    )


def _utilization(w3: Web3, symbol: str) -> Optional[float]:
    """varDebtToken.totalSupply() / aToken.totalSupply() — pool utilization ratio."""
    canonical = next(
        (name for name in _ATOKEN if name.casefold() == str(symbol).casefold()),
        symbol,
    )
    a_addr = _ATOKEN.get(canonical)
    d_addr = _VARDEBT.get(canonical)
    if not a_addr or not d_addr:
        return None
    try:
        a_tok = w3.eth.contract(
            address=Web3.to_checksum_address(a_addr), abi=_TOTAL_SUPPLY_ABI
        )
        d_tok = w3.eth.contract(
            address=Web3.to_checksum_address(d_addr), abi=_TOTAL_SUPPLY_ABI
        )
        a_supply = a_tok.functions.totalSupply().call()
        d_supply = d_tok.functions.totalSupply().call()
        if a_supply <= 0:
            return None
        return d_supply / a_supply
    except Exception as e:
        log.debug("utilization error for %s: %s", symbol, e)
        return None


def _reserve_flags(w3: Web3, symbol: str) -> tuple[Optional[bool], Optional[bool]]:
    """
    Returns (is_frozen, is_paused) for an Aave v3 reserve on Base.

    Reads the ReserveConfigurationMap packed uint256 via getReserveData(address).
    The first 32 bytes of the return data are configuration.data.
    Bit layout (Aave v3 source):
      bit 56 = isActive
      bit 57 = isFrozen   — no new supply/borrow; flash loans blocked
      bit 58 = borrowingEnabled
      bit 60 = isPaused   — ALL operations blocked (repay, withdraw, etc.)

    Returns (None, None) if the asset is unknown or the RPC call fails.
    """
    addr = asset_address(symbol)
    if not addr:
        return None, None
    try:
        selector = Web3.keccak(text="getReserveData(address)")[:4]
        from eth_abi import encode as abi_encode

        encoded = abi_encode(["address"], [Web3.to_checksum_address(addr)])
        result = w3.eth.call(
            {
                "to": Web3.to_checksum_address(AAVE_POOL_BASE),
                "data": "0x" + (selector + encoded).hex(),
            }
        )
        # ReserveData struct has no dynamic fields; first word is configuration.data
        config = int.from_bytes(result[:32], "big")
        is_frozen = bool((config >> 57) & 1)
        is_paused = bool((config >> 60) & 1)
        if is_frozen or is_paused:
            log.warning(
                "reserve flags for %s: frozen=%s paused=%s",
                symbol,
                is_frozen,
                is_paused,
            )
        return is_frozen, is_paused
    except Exception as e:
        log.debug("reserve_flags error for %s: %s", symbol, e)
        return None, None


def _reserve_configuration(w3: Web3, symbol: str) -> Optional[dict]:
    """Decode the live Aave reserve configuration from ``getReserveData``."""
    addr = asset_address(symbol)
    if not addr:
        return None
    try:
        selector = Web3.keccak(text="getReserveData(address)")[:4]
        from eth_abi import encode as abi_encode

        result = w3.eth.call(
            {
                "to": Web3.to_checksum_address(AAVE_POOL_BASE),
                "data": "0x"
                + (
                    selector + abi_encode(["address"], [Web3.to_checksum_address(addr)])
                ).hex(),
            }
        )
        if len(result) < 32:
            return None
        config = int.from_bytes(result[:32], "big")
        return {
            "ltv": ((config >> 0) & 0xFFFF) / 10_000,
            "liquidation_threshold": ((config >> 16) & 0xFFFF) / 10_000,
            "liquidation_bonus": ((config >> 32) & 0xFFFF) / 10_000,
            "decimals": (config >> 48) & 0xFF,
            "active": bool((config >> 56) & 1),
            "frozen": bool((config >> 57) & 1),
            "borrowing_enabled": bool((config >> 58) & 1),
            "paused": bool((config >> 60) & 1),
            "flashloan_enabled": bool((config >> 63) & 1),
            "emode_category": (config >> 168) & 0xFF,
        }
    except Exception as e:
        log.debug("reserve configuration error for %s: %s", symbol, e)
        return None


def _risk_snapshot(w3: Web3, asset: str, user_address: Optional[str]) -> dict:
    """Read reserve risk plus account/eMode risk without making the cycle fail."""
    reserve = _reserve_configuration(w3, asset)
    result: dict = {
        "available": reserve is not None,
        "reserve_ltv": reserve.get("ltv") if reserve else None,
        "reserve_liquidation_threshold": (
            reserve.get("liquidation_threshold") if reserve else None
        ),
        "reserve_emode_category": reserve.get("emode_category") if reserve else None,
        "account_ltv": None,
        "account_liquidation_threshold": None,
        "user_emode_category": None,
        "emode_liquidation_threshold": None,
        "risk_block": None,
    }
    try:
        result["risk_block"] = w3.eth.block_number
    except Exception:
        pass

    if not user_address or not Web3.is_address(user_address):
        return result

    result["available"] = False
    try:
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(AAVE_POOL_BASE), abi=_POOL_RISK_ABI
        )
        user = Web3.to_checksum_address(user_address)
        account = pool.functions.getUserAccountData(user).call()
        result["account_ltv"] = float(account[4]) / 10_000
        result["account_liquidation_threshold"] = float(account[3]) / 10_000
        category = int(pool.functions.getUserEMode(user).call())
        result["user_emode_category"] = category
        if category > 0:
            category_data = pool.functions.getEModeCategoryData(category).call()
            result["emode_liquidation_threshold"] = float(category_data[1]) / 10_000
        result["available"] = True
    except Exception as e:
        # Reserve config is still useful for new-position simulation when the
        # account has no debt or the optional user call is unavailable.
        log.debug("account risk configuration error: %s", e)
    return result


def _recent_liquidations(w3: Web3, lookback: int) -> Optional[int]:
    """Count LiquidationCall events on the Aave v3 pool within the last `lookback` blocks.
    Uses raw httpx calls to bypass web3.py serialization and ensure hex block params."""
    topic = (
        "0x"
        + Web3.keccak(
            text="LiquidationCall(address,address,address,uint256,uint256,address,bool)"
        ).hex()
    )
    rpc_url = str(w3.provider.endpoint_uri)
    try:
        latest_hex = httpx.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            timeout=10,
        ).json()["result"]
        latest_int = int(latest_hex, 16)
        from_hex = hex(latest_int - (lookback - 1))

        resp = httpx.post(
            rpc_url,
            json={
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [
                    {
                        "address": Web3.to_checksum_address(AAVE_POOL_BASE),
                        "fromBlock": from_hex,
                        "toBlock": latest_hex,
                        "topics": [topic],
                    }
                ],
                "id": 2,
            },
            timeout=10,
        ).json()
        if "error" in resp:
            log.debug("liquidation log RPC error: %s", resp["error"])
            return None
        return len(resp.get("result", []))
    except Exception as e:
        log.debug("liquidation log error: %s", e)
        return None

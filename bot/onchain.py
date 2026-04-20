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

All fields are Optional — if the RPC call fails we return None and the
corresponding filter in filters.py is simply skipped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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

_TOTAL_SUPPLY_ABI = [
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
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
    # Reserve status flags — None means RPC call failed (treated as safe/unknown)
    asset_frozen: Optional[bool] = None  # supply asset frozen (no new borrows/deposits)
    asset_paused: Optional[bool] = None  # supply asset paused (all ops blocked)
    borrow_asset_frozen: Optional[bool] = None  # borrow asset (USDC) frozen
    borrow_asset_paused: Optional[bool] = None  # borrow asset (USDC) paused


def fetch(
    asset: str,
    rpc_url: str = "https://mainnet.base.org",
    lookback_blocks: int = _LOOKBACK_BLOCKS_DEFAULT,
    borrow_asset: str = "USDC",
) -> OnChainData:
    """
    Fetch on-chain Aave v3 state from Base. Never raises — returns None fields on error.
    A single Web3 connection is created per call (read-only, no wallet needed).
    """
    usdc_util = asset_util = recent_liq = None
    asset_frozen = asset_paused = borrow_frozen = borrow_paused = None
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

    except Exception as e:
        log.debug("onchain.fetch error: %s", e)

    return OnChainData(
        usdc_utilization=usdc_util,
        asset_utilization=asset_util,
        recent_liquidations=recent_liq,
        asset_frozen=asset_frozen,
        asset_paused=asset_paused,
        borrow_asset_frozen=borrow_frozen,
        borrow_asset_paused=borrow_paused,
    )


def _utilization(w3: Web3, symbol: str) -> Optional[float]:
    """varDebtToken.totalSupply() / aToken.totalSupply() — pool utilization ratio."""
    a_addr = _ATOKEN.get(symbol)
    d_addr = _VARDEBT.get(symbol)
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
    addr = _ASSET_ADDR.get(symbol)
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

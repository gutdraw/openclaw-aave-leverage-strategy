"""
On-chain data fetcher — reads Aave v3 pool state directly from Base.

Two signals, both read-only (eth_call / eth_getLogs), free public RPC:

  1. USDC utilization  — varDebtToken.totalSupply() / aToken.totalSupply()
     Aave's interest rate curve has a sharp kink at the optimal utilization
     point (~90%). Above the kink borrow APR climbs steeply. At 92% the
     variable rate is already well above typical — a lagging indicator in
     the MCP borrow_apr is too slow to catch this in real-time.

  2. Recent liquidations — LiquidationCall events in the last ~5 minutes.
     A spike in liquidations means the market is under stress. Opening a
     new leveraged position into a cascade amplifies risk.

Both fields are Optional — if the RPC call fails we return None and the
corresponding filter in filters.py is simply skipped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from web3 import Web3

log = logging.getLogger(__name__)

# ── Aave v3 Base contract addresses (verified from wallet_reader.py) ──────────
AAVE_POOL_BASE = "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"

# aToken and variableDebtToken addresses per asset
_ATOKEN: dict[str, str] = {
    "WETH":   "0xD4a0e0b9149BCee3C920d2E00b5dE09138fd8bb7",
    "wstETH": "0x99CBC45ea5bb7EF3a5BC08FB1B7E56Bb2442Ef0D",
    "cbBTC":  "0xBdB9300b7CDE636d9CD4AFF00f6F009fFBBc8eE6",
    "USDC":   "0x4e65fE4DbA92790696d040ac24Aa414708F5c0AB",
}
_VARDEBT: dict[str, str] = {
    "WETH":   "0x24e6e0795b3c7c71D965fCc4f371803d1c1DcA1E",
    "wstETH": "0x41A7C3f5904ad176dACbb1D99101F59ef0811DC1",
    "cbBTC":  "0x05E08702028De6AAD395Dc6478B554a56920B9AD",
    "USDC":   "0x59dca05b6c26dbd64b5381374aaac5cd05644c28",
}

_TOTAL_SUPPLY_ABI = [{
    "inputs": [],
    "name": "totalSupply",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}]

# Base produces a block ~every 2 seconds; 150 blocks ≈ 5 minutes
_LOOKBACK_BLOCKS = 150


@dataclass
class OnChainData:
    usdc_utilization: Optional[float]   # 0.0–1.0  e.g. 0.88 = 88% utilised
    asset_utilization: Optional[float]  # supply-side utilization of the trade asset
    recent_liquidations: Optional[int]  # LiquidationCall count in last ~5 min


def fetch(asset: str, rpc_url: str = "https://mainnet.base.org") -> OnChainData:
    """
    Fetch on-chain Aave v3 state from Base. Never raises — returns None fields on error.
    A single Web3 connection is created per call (read-only, no wallet needed).
    """
    usdc_util = asset_util = recent_liq = None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))

        usdc_util  = _utilization(w3, "USDC")
        asset_util = _utilization(w3, asset)
        recent_liq = _recent_liquidations(w3)

    except Exception as e:
        log.debug("onchain.fetch error: %s", e)

    return OnChainData(
        usdc_utilization=usdc_util,
        asset_utilization=asset_util,
        recent_liquidations=recent_liq,
    )


def _utilization(w3: Web3, symbol: str) -> Optional[float]:
    """varDebtToken.totalSupply() / aToken.totalSupply() — pool utilization ratio."""
    a_addr = _ATOKEN.get(symbol)
    d_addr = _VARDEBT.get(symbol)
    if not a_addr or not d_addr:
        return None
    try:
        a_tok = w3.eth.contract(address=Web3.to_checksum_address(a_addr), abi=_TOTAL_SUPPLY_ABI)
        d_tok = w3.eth.contract(address=Web3.to_checksum_address(d_addr), abi=_TOTAL_SUPPLY_ABI)
        a_supply = a_tok.functions.totalSupply().call()
        d_supply = d_tok.functions.totalSupply().call()
        if a_supply <= 0:
            return None
        return d_supply / a_supply
    except Exception as e:
        log.debug("utilization error for %s: %s", symbol, e)
        return None


def _recent_liquidations(w3: Web3) -> Optional[int]:
    """Count LiquidationCall events on the Aave v3 pool in the last ~5 minutes.
    Falls back to a smaller block range if the RPC rejects a wide getLogs request."""
    topic = "0x" + Web3.keccak(
        text="LiquidationCall(address,address,address,uint256,uint256,address,bool)"
    ).hex()
    for lookback in (_LOOKBACK_BLOCKS, 50, 20):
        try:
            latest = w3.eth.block_number
            logs = w3.eth.get_logs({
                "address": Web3.to_checksum_address(AAVE_POOL_BASE),
                "fromBlock": latest - lookback,
                "toBlock":   latest,
                "topics":    [topic],
            })
            return len(logs)
        except Exception as e:
            log.debug("liquidation log error (lookback=%d): %s", lookback, e)
    return None

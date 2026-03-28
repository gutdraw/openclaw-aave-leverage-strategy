"""
Thin MCP JSON-RPC 2.0 client for the aave-leverage-agent server.

Handles x402 session auto-renewal: when a call returns 401/402, the client
signs a new EIP-712 TransferWithAuthorization using the configured private key,
purchases a new monthly session, updates the in-memory token, persists it back
to the config file, and retries the original call once.

If no private key is configured (paper mode with no key), the 401/402 is raised
as a SessionExpiredError for the caller to handle.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# x402 payment constants (Base mainnet USDC)
_BASE_CHAIN  = 8453
_USDC_BASE   = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_USDC_NAME   = "USD Coin"
_USDC_VER    = "2"
_VALID_DURATIONS = {"hour", "day", "week", "month"}


class SessionExpiredError(RuntimeError):
    """Raised when the MCP session has expired and cannot be auto-renewed."""


@dataclass
class MCPClient:
    base_url: str         # e.g. https://aave-leverage-agent-production.up.railway.app
    session_token: str    # Bearer token from POST /mcp/auth
    wallet_address: str
    private_key: str = ""             # required for auto-renewal; empty = no renewal
    config_path: Optional[str] = None # if set, persists renewed token back to config file
    session_duration: str = "month"   # hour | day | week | month

    def call(self, tool: str, args: dict) -> dict:
        """Call a single MCP tool. Auto-renews session on 401/402 if private_key set."""
        try:
            return self._call_once(tool, args)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 402):
                log.warning("MCP session expired (HTTP %d) — attempting renewal",
                            e.response.status_code)
                self._renew_session()
                return self._call_once(tool, args)
            raise

    def _call_once(self, tool: str, args: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        headers = {
            "Authorization": f"Bearer {self.session_token}",
            "X-Wallet-Address": self.wallet_address,
            "Content-Type": "application/json",
        }
        resp = httpx.post(
            f"{self.base_url}/mcp", json=payload, headers=headers, timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"MCP error: {result['error']}")
        return json.loads(result["result"]["content"][0]["text"])

    def _renew_session(self) -> None:
        """
        Purchase a new MCP session via x402 EIP-712 TransferWithAuthorization.
        Updates self.session_token and persists to config file if config_path set.
        Raises SessionExpiredError if no private key is available.
        """
        pk = self.private_key or os.environ.get("PRIVATE_KEY", "")
        if not pk:
            raise SessionExpiredError(
                "MCP session expired and no private_key configured for auto-renewal. "
                "Run scripts/buy_session.py manually to get a new token."
            )

        # Lazy import — only needed for live/renewal path
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        duration = self.session_duration if self.session_duration in _VALID_DURATIONS else "month"
        log.info("Purchasing new MCP session (duration=%s)…", duration)

        # ── Step 1: request the 402 challenge ────────────────────────────────
        r = httpx.post(
            f"{self.base_url}/mcp/auth",
            params={"duration": duration},
            json={"wallet_address": self.wallet_address},
            timeout=15,
        )

        if r.status_code == 200:
            # Dev-mode server with payment bypass
            self.session_token = r.json()["session_token"]
            self._persist_token()
            return

        if r.status_code != 402:
            raise SessionExpiredError(
                f"Unexpected response from /mcp/auth: {r.status_code} {r.text}"
            )

        challenge = r.json()
        accepts   = challenge.get("accepts", [{}])[0]
        pay_to    = accepts["payTo"]
        amount    = int(accepts["maxAmountRequired"])
        asset     = accepts.get("asset", _USDC_BASE)

        log.info("Signing x402 payment: %.2f USDC → %s", amount / 1_000_000, pay_to)

        # ── Step 2: sign EIP-712 TransferWithAuthorization ───────────────────
        now          = int(time.time())
        valid_before = now + 120
        nonce        = "0x" + secrets.token_hex(32)

        domain = {
            "name":              _USDC_NAME,
            "version":           _USDC_VER,
            "chainId":           _BASE_CHAIN,
            "verifyingContract": asset,
        }
        message_types = {
            "TransferWithAuthorization": [
                {"name": "from",        "type": "address"},
                {"name": "to",          "type": "address"},
                {"name": "value",       "type": "uint256"},
                {"name": "validAfter",  "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce",       "type": "bytes32"},
            ],
        }
        message = {
            "from":        self.wallet_address,
            "to":          pay_to,
            "value":       amount,
            "validAfter":  0,
            "validBefore": valid_before,
            "nonce":       nonce,
        }

        encoded = encode_typed_data(domain, message_types, message)
        signed  = Account.sign_message(encoded, private_key=pk)
        sig_hex = signed.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex

        # ── Step 3: retry with X-PAYMENT header ──────────────────────────────
        payment_header = json.dumps({
            "x402Version": 1,
            "scheme":      "exact",
            "network":     "base-mainnet",
            "payload": {
                "authorization": {
                    "from":        self.wallet_address,
                    "to":          pay_to,
                    "value":       str(amount),
                    "validAfter":  "0",
                    "validBefore": str(valid_before),
                    "nonce":       nonce,
                },
                "signature": sig_hex,
            },
        })

        r2 = httpx.post(
            f"{self.base_url}/mcp/auth",
            params={"duration": duration},
            json={"wallet_address": self.wallet_address},
            headers={"X-PAYMENT": payment_header},
            timeout=30,
        )

        if r2.status_code != 200:
            raise SessionExpiredError(
                f"x402 payment rejected ({r2.status_code}): {r2.text}. "
                "Check wallet USDC balance on Base."
            )

        self.session_token = r2.json()["session_token"]
        log.info("MCP session renewed successfully")
        self._persist_token()

    def _persist_token(self) -> None:
        """Write updated session_token back to config file if config_path is set."""
        if not self.config_path:
            return
        try:
            import yaml
            path = Path(self.config_path)
            cfg  = yaml.safe_load(path.read_text())
            cfg["mcp_session_token"] = self.session_token
            path.write_text(yaml.dump(cfg, default_flow_style=False))
            log.info("Session token persisted to %s", self.config_path)
        except Exception as e:
            log.warning("Could not persist session token to config: %s", e)

    # ── Tool wrappers ────────────────────────────────────────────────────────

    def get_position(self) -> dict:
        return self.call("get_position", {"user_address": self.wallet_address})

    def prepare_open(self, leverage: float, amount: float,
                     supply_asset: str, borrow_asset: str) -> dict:
        return self.call("prepare_open", {
            "user_address":  self.wallet_address,
            "leverage":      leverage,
            "amount":        amount,
            "supply_asset":  supply_asset,
            "borrow_asset":  borrow_asset,
        })

    def prepare_close(self, position_id: str) -> dict:
        return self.call("prepare_close", {
            "user_address": self.wallet_address,
            "position_id":  position_id,
        })

    def prepare_reduce(self, supply_asset: str, borrow_asset: str,
                       target_leverage: float) -> dict:
        return self.call("prepare_reduce", {
            "user_address":   self.wallet_address,
            "supply_asset":   supply_asset,
            "borrow_asset":   borrow_asset,
            "target_leverage": target_leverage,
        })

    def prepare_increase(self, leverage: float, amount: float,
                         supply_asset: str, borrow_asset: str) -> dict:
        return self.call("prepare_increase", {
            "user_address": self.wallet_address,
            "leverage":     leverage,
            "amount":       amount,
            "supply_asset": supply_asset,
            "borrow_asset": borrow_asset,
        })

    def swap(self, token_in: str, token_out: str, amount_in: float) -> dict:
        return self.call("swap", {
            "user_address": self.wallet_address,
            "token_in":     token_in,
            "token_out":    token_out,
            "amount_in":    str(amount_in),
        })

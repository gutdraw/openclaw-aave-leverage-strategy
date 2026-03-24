#!/usr/bin/env python3
"""
buy_session.py — purchase an MCP session token via x402 (EIP-3009 USDC payment).

Usage:
    python3 scripts/buy_session.py --wallet 0xYOUR_WALLET --duration week

    # Private key via env var (recommended)
    export PRIVATE_KEY=0xYOUR_PRIVATE_KEY
    python3 scripts/buy_session.py --wallet 0xYOUR_WALLET --duration week

    # Or inline (key will appear in shell history — less safe)
    python3 scripts/buy_session.py --wallet 0x... --key 0x... --duration week

Durations and prices:
    hour   $0.05    1-hour session   (quick test)
    day    $0.25    24-hour session
    week   $1.50    7-day session    (recommended for bots)
    month  $4.00    30-day session

Requirements:
    - Wallet must hold enough USDC on Base (contract: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913)
    - pip install web3 eth-account requests   (already in requirements.txt)
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

MCP_URL     = "https://aave-leverage-agent-production.up.railway.app"
BASE_CHAIN  = 8453
USDC_BASE   = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_NAME   = "USD Coin"
USDC_VER    = "2"


def buy(wallet: str, private_key: str, duration: str = "week") -> str:
    """
    Complete the x402 payment flow and return the session token.

    1. POST /mcp/auth?duration=<duration>  →  402 challenge
    2. Sign EIP-712 TransferWithAuthorization
    3. Retry with X-PAYMENT header  →  session_token
    """

    # ── Step 1: request the 402 challenge ────────────────────────────────────
    r = requests.post(
        f"{MCP_URL}/mcp/auth",
        params={"duration": duration},
        json={"wallet_address": wallet},
        timeout=15,
    )

    if r.status_code != 402:
        if r.status_code == 200:
            # Dev-mode server bypassed payment — return token directly
            return r.json()["session_token"]
        raise RuntimeError(f"Expected 402, got {r.status_code}: {r.text}")

    challenge = r.json()

    # Parse the x402 challenge
    accepts = challenge.get("accepts", [{}])[0]
    pay_to  = accepts["payTo"]
    amount  = int(accepts["maxAmountRequired"])   # USDC atomic units (6 decimals)
    asset   = accepts.get("asset", USDC_BASE)

    print(f"  Payment required: {amount / 1_000_000:.2f} USDC → {pay_to}")

    # ── Step 2: sign EIP-712 TransferWithAuthorization ────────────────────────
    now          = int(time.time())
    valid_after  = 0
    valid_before = now + 120          # 2-minute window
    nonce        = "0x" + secrets.token_hex(32)

    domain = {
        "name":              USDC_NAME,
        "version":           USDC_VER,
        "chainId":           BASE_CHAIN,
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
        "from":        wallet,
        "to":          pay_to,
        "value":       amount,
        "validAfter":  valid_after,
        "validBefore": valid_before,
        "nonce":       nonce,
    }

    encoded = encode_typed_data(domain, message_types, message)
    signed  = Account.sign_message(encoded, private_key=private_key)
    sig_hex = signed.signature.hex()
    if not sig_hex.startswith("0x"):
        sig_hex = "0x" + sig_hex

    # ── Step 3: retry with X-PAYMENT header ──────────────────────────────────
    payment_header = json.dumps({
        "x402Version": 1,
        "scheme": "exact",
        "network": "base-mainnet",
        "payload": {
            "authorization": {
                "from":        wallet,
                "to":          pay_to,
                "value":       str(amount),
                "validAfter":  str(valid_after),
                "validBefore": str(valid_before),
                "nonce":       nonce,
            },
            "signature": sig_hex,
        },
    })

    r2 = requests.post(
        f"{MCP_URL}/mcp/auth",
        params={"duration": duration},
        json={"wallet_address": wallet},
        headers={"X-PAYMENT": payment_header},
        timeout=30,
    )

    if r2.status_code != 200:
        raise RuntimeError(f"Payment rejected ({r2.status_code}): {r2.text}")

    return r2.json()["session_token"]


def main():
    parser = argparse.ArgumentParser(description="Purchase an MCP session token via x402")
    parser.add_argument("--wallet",   required=True, help="Your bot wallet address (0x...)")
    parser.add_argument("--duration", default="week",
                        choices=["hour", "day", "week", "month"],
                        help="Session duration (default: week = $1.50)")
    parser.add_argument("--key",      default=None,
                        help="Private key (prefer PRIVATE_KEY env var)")
    parser.add_argument("--config",   default=None,
                        help="Optional: write token directly into this config file")
    args = parser.parse_args()

    private_key = args.key or os.environ.get("PRIVATE_KEY")
    if not private_key:
        print("Error: provide --key or set PRIVATE_KEY env var", file=sys.stderr)
        sys.exit(1)

    print(f"Purchasing {args.duration} session for {args.wallet}...")

    try:
        token = buy(args.wallet, private_key, args.duration)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\nSession token:\n  {token}\n")

    # Optionally write directly into a config file
    if args.config:
        import yaml
        from pathlib import Path
        cfg_path = Path(args.config)
        cfg = yaml.safe_load(cfg_path.read_text())
        cfg["mcp_session_token"] = token
        cfg_path.write_text(yaml.dump(cfg, default_flow_style=False))
        print(f"Written to {args.config}")
    else:
        print("Add to my-config.yml:")
        print(f"  mcp_session_token: \"{token}\"")


if __name__ == "__main__":
    main()

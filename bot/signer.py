"""
Transaction signer for live trading mode.

All MCP prepare_* tools return a consistent shape:

    {
        "transaction_steps": [
            {
                "contract": "0xAddress",
                "abi_fn":   "functionName(type1,type2,...)",   # full ABI sig
                "args":     [val1, val2, ...],                  # flat or tuple-as-list
                "gas":      N,
                "title":    "human readable",
            },
            ...
        ]
    }

approve / approveDelegation steps are always included by the server.
The signer checks the on-chain allowance before each approval step and
skips it when the existing allowance is already sufficient — avoiding the
Base sequencer's "in-flight tx limit for delegated accounts" error.

Architecture (Base mainnet):
  LeverageRouterV3  (0x4A60C1E7d78DA2A61007fE21d282a859D3906724) — caller
  LeverageVaultV3   (0xf2A51d441E6bA96c37fD0024115DccF03764478f) — approve target
"""
from __future__ import annotations

import logging

from eth_account import Account
from web3 import Web3

log = logging.getLogger(__name__)

_UINT256_MAX = 2**256 - 1


def _split_sig_types(sig: str) -> list[str]:
    """
    Parse parameter types from a function signature or tuple type string,
    correctly handling nested tuples.

      "approve(address,uint256)"
          → ["address", "uint256"]
      "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
          → ["(address,address,uint24,address,uint256,uint256,uint160)"]
      "(address,address,uint24)"
          → ["address", "address", "uint24"]
    """
    start = sig.index("(") + 1
    end   = sig.rindex(")")
    inner = sig[start:end]
    if not inner:
        return []
    types: list[str] = []
    depth   = 0
    current = ""
    for ch in inner:
        if ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            types.append(current.strip())
            current = ""
        else:
            current += ch
    if current:
        types.append(current.strip())
    return types


class Signer:
    def __init__(self, rpc_url: str, private_key: str) -> None:
        self.w3      = Web3(Web3.HTTPProvider(rpc_url))
        self.account = Account.from_key(private_key)
        self._nonce: int | None = None

    @property
    def address(self) -> str:
        return self.account.address

    # ── Nonce management ──────────────────────────────────────────────────────

    def _next_nonce(self) -> int:
        """
        Local nonce counter — initialised once from get_transaction_count("pending"),
        then incremented in-process. Avoids RPC race between consecutive steps.
        """
        if self._nonce is None:
            self._nonce = self.w3.eth.get_transaction_count(
                self.account.address, "pending"
            )
            log.debug("nonce initialised to %d", self._nonce)
        n = self._nonce
        self._nonce += 1
        return n

    def reset_nonce(self) -> None:
        """Force nonce re-fetch on next use (call after a failed/reverted tx)."""
        self._nonce = None

    # ── On-chain reads ────────────────────────────────────────────────────────

    def _erc20_allowance(self, token: str, owner: str, spender: str) -> int:
        """allowance(owner, spender) via raw eth_call — no ABI file needed."""
        selector = Web3.keccak(text="allowance(address,address)")[:4]
        from eth_abi import encode as abi_encode, decode as abi_decode
        encoded  = abi_encode(["address", "address"], [owner, spender])
        result   = self.w3.eth.call({"to": token, "data": "0x" + (selector + encoded).hex()})
        return int(abi_decode(["uint256"], result)[0])

    def _should_skip_approval(self, step: dict) -> bool:
        """
        Return True if this approve / approveDelegation step can be skipped
        because the on-chain allowance is already >= the requested amount.
        Skipping avoids an extra in-flight tx which hits Base sequencer limits
        on accounts that have Aave credit delegation.
        """
        fn_sig   = step.get("abi_fn", "")
        fn_name  = fn_sig.split("(")[0]
        args     = step.get("args", [])
        contract = step.get("contract", "")

        if fn_name not in ("approve", "approveDelegation") or len(args) < 2:
            return False

        spender = args[0]
        amount  = self._coerce_arg("uint256", args[1])
        current = self._erc20_allowance(contract, self.address, spender)
        if current >= amount:
            log.info(
                "%s already sufficient (have=%d need=%d) on %s — skipping",
                fn_name, current, amount, contract,
            )
            return True
        return False

    # ── Main entry point ──────────────────────────────────────────────────────

    def execute_steps(self, resp: dict) -> str:
        """
        Execute all transaction steps from an MCP response.
        Returns the last tx hash sent.

        Accepted response shapes (in priority order):
          1. {"transaction_steps": [...]}  — all prepare_* and swap tools
          2. {"transaction": {...}}        — legacy single raw-tx shape
        """
        if "transaction_steps" in resp:
            steps = resp["transaction_steps"]
        elif "transaction" in resp:
            t = resp["transaction"]
            steps = [t] if isinstance(t, dict) else [{"to": t}]
        else:
            raise KeyError(
                f"MCP response has no recognised transaction format. "
                f"Keys present: {list(resp.keys())}"
            )

        last_hash = None
        for step in steps:
            if self._should_skip_approval(step):
                continue
            tx = self._step_to_raw_tx(step)
            try:
                last_hash = self.sign_and_send(tx)
            except Exception:
                self.reset_nonce()
                raise
            log.info("tx %s sent (%s)", last_hash, step.get("title", step.get("type", "?")))
            self.wait_for_receipt(last_hash)
        return last_hash

    # ── Step encoding ─────────────────────────────────────────────────────────

    def _coerce_arg(self, type_str: str, value):
        """
        Coerce a Python value from JSON representation to what eth_abi expects.

        Solidity → Python mapping:
          address         → str "0x..."  (eth_abi handles checksum internally)
          uint*/int*      → int          (converts "123" or "0x..." strings)
          bytes / bytesN  → bytes        (converts "0x..." hex strings; "0x" → b"")
          bool            → bool
          (T1,T2,...)     → tuple        (list or tuple; elements coerced recursively)
        """
        t = type_str.strip()
        # Check tuple FIRST — "uint" is a substring of "(address,uint256)"
        if t.startswith("("):
            # Tuple/struct — parse sub-types and coerce each element recursively
            sub_types = _split_sig_types(t)
            if isinstance(value, (list, tuple)):
                return tuple(
                    self._coerce_arg(st, v) for st, v in zip(sub_types, value)
                )
            return value
        elif t == "address":
            return Web3.to_checksum_address(value) if isinstance(value, str) else value
        elif t == "bool":
            if isinstance(value, str):
                return value.lower() not in ("0", "false", "")
            return bool(value)
        elif "uint" in t or "int" in t:
            if isinstance(value, str):
                return int(value, 16) if value.startswith("0x") else int(value)
            return int(value)
        elif t == "bytes" or (t.startswith("bytes") and t[5:].isdigit()):
            if isinstance(value, (bytes, bytearray)):
                return value
            if isinstance(value, str):
                hex_val = value[2:] if value.startswith("0x") else value
                return bytes.fromhex(hex_val)
            return value
        return value

    def _step_to_raw_tx(self, step: dict) -> dict:
        """
        Convert an MCP step dict to a raw EIP-1559 tx dict.

        Handles:
          1. Raw tx:    {"to": "0x...", "data": "0x...", "gas": N, ...}
          2. ABI step:  {"contract": "0x...", "abi_fn": "fn(types)", "args": [...]}
          3. Hex data:  {"contract": "0x...", "calldata": "0x...", "gas": N}
        """
        if "to" in step:
            return step

        contract = step.get("contract")
        gas      = step.get("gas", 500_000)
        value    = int(step.get("eth_value") or step.get("value") or 0)

        if "calldata" in step and isinstance(step["calldata"], str):
            return {"to": contract, "data": step["calldata"], "value": value, "gas": gas}

        fn_sig   = step.get("abi_fn", "")
        args_raw = step.get("args", [])
        types    = _split_sig_types(fn_sig)

        if types:
            from eth_abi import encode as abi_encode
            coerced      = [self._coerce_arg(t, a) for t, a in zip(types, args_raw)]
            selector     = Web3.keccak(text=fn_sig)[:4]
            encoded_args = abi_encode(types, coerced)
            data         = "0x" + (selector + encoded_args).hex()
        else:
            selector = Web3.keccak(text=fn_sig)[:4]
            data     = "0x" + selector.hex()

        return {"to": contract, "data": data, "value": value, "gas": gas}

    # ── Signing and broadcast ─────────────────────────────────────────────────

    def sign_and_send(self, tx: dict) -> str:
        """Sign and broadcast a raw transaction dict. Returns the tx hash."""
        tx = dict(tx)
        tx.setdefault("from", self.account.address)
        tx.setdefault("chainId", self.w3.eth.chain_id)
        if "nonce" not in tx:
            tx["nonce"] = self._next_nonce()
            log.info(
                "using nonce %d (latest=%d pending=%d)",
                tx["nonce"],
                self.w3.eth.get_transaction_count(self.account.address, "latest"),
                self.w3.eth.get_transaction_count(self.account.address, "pending"),
            )

        if "maxFeePerGas" not in tx and "gasPrice" not in tx:
            latest   = self.w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas", self.w3.to_wei(0.1, "gwei"))
            priority = self.w3.to_wei(0.005, "gwei")  # 0.005 gwei tip — sufficient on Base (base fee ~0.001 gwei)
            tx["maxPriorityFeePerGas"] = priority
            tx["maxFeePerGas"]         = base_fee * 2 + priority

        signed  = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def wait_for_receipt(self, tx_hash: str, timeout: int = 120) -> dict:
        """Block until the tx is mined, raise if it reverted."""
        receipt = dict(
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        )
        if receipt.get("status") == 0:
            raise RuntimeError(f"tx reverted on-chain: {tx_hash}")
        return receipt

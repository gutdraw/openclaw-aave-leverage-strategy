"""
Transaction signer for live trading mode.

Wraps web3.py to sign and broadcast the raw transaction returned by
an MCP prepare_* call.  Paper-trading callers never import this path.
"""
from __future__ import annotations

from eth_account import Account
from web3 import Web3


class Signer:
    def __init__(self, rpc_url: str, private_key: str) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.account = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self.account.address

    def sign_and_send(self, tx: dict) -> str:
        """
        Sign and broadcast a raw transaction dict.

        The MCP server returns a tx dict with keys:
          to, data, value, gas, maxFeePerGas, maxPriorityFeePerGas (EIP-1559)
        or gasPrice (legacy).

        Returns the transaction hash (hex string).
        """
        tx.setdefault("from", self.account.address)
        tx.setdefault("chainId", self.w3.eth.chain_id)
        if "nonce" not in tx:
            tx["nonce"] = self.w3.eth.get_transaction_count(self.account.address)

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()

    def wait_for_receipt(self, tx_hash: str, timeout: int = 120) -> dict:
        """Block until the tx is mined and return the receipt."""
        return dict(
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        )

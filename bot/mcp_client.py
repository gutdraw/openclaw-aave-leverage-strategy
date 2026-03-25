"""
Thin MCP JSON-RPC 2.0 client for the aave-leverage-agent server.
No business logic — pure transport layer.
"""
import json
import httpx
from dataclasses import dataclass


@dataclass
class MCPClient:
    base_url: str        # e.g. https://aave-leverage-agent-production.up.railway.app
    session_token: str   # Bearer token from POST /mcp/auth
    wallet_address: str

    def call(self, tool: str, args: dict) -> dict:
        """Call a single MCP tool. Raises on non-200 or JSON-RPC error."""
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
        # MCP returns content as JSON-encoded string in result.content[0].text
        return json.loads(result["result"]["content"][0]["text"])

    def get_position(self) -> dict:
        return self.call("get_position", {"user_address": self.wallet_address})

    def prepare_open(self, leverage: float, amount: float,
                     supply_asset: str, borrow_asset: str) -> dict:
        return self.call("prepare_open", {
            "user_address": self.wallet_address,
            "leverage": leverage,
            "amount": amount,
            "supply_asset": supply_asset,
            "borrow_asset": borrow_asset,
        })

    def prepare_close(self, position_id: str) -> dict:
        return self.call("prepare_close", {
            "user_address": self.wallet_address,
            "position_id": position_id,
        })

    def prepare_reduce(self, supply_asset: str, borrow_asset: str,
                       target_leverage: float) -> dict:
        return self.call("prepare_reduce", {
            "user_address": self.wallet_address,
            "supply_asset": supply_asset,
            "borrow_asset": borrow_asset,
            "target_leverage": target_leverage,
        })

    def swap(self, token_in: str, token_out: str, amount_in: float) -> dict:
        return self.call("swap", {
            "user_address": self.wallet_address,
            "token_in": token_in,
            "token_out": token_out,
            "amount_in": str(amount_in),
        })

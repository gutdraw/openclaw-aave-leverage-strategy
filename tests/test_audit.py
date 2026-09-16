import json

import bot.audit as audit
from web3 import Web3

from bot.audit import ReadOnlyRpc, build_report, load_journal_records, normalize_tx_hash


def _hash(character: str) -> str:
    return character * 64


def test_normalize_tx_hash_accepts_common_serializations() -> None:
    value = _hash("a")
    assert normalize_tx_hash(value) == value
    assert normalize_tx_hash("0x" + value) == value
    assert normalize_tx_hash(f"HexBytes('0x{value}')") == value
    assert normalize_tx_hash(repr(bytes.fromhex(value))) == value
    assert normalize_tx_hash("not-a-hash") is None


def test_audit_classifies_flat_hold_cycles_and_keeps_pnl_unproven() -> None:
    opening_hash = _hash("a")
    closing_hash = _hash("b")
    execution_id = "open-execution"
    entries = [
        {
            "type": "cycle",
            "ts": "2026-01-01T00:00:00Z",
            "signal": "strong_long",
            "decision": "open_long",
        },
        {
            "type": "trade",
            "action": "open",
            "ts": "2026-01-01T00:00:00Z",
            "asset": "WETH",
            "direction": "long",
            "position_id": "WETH/USDC",
            "entry_price": 2_000.0,
            "supply": 1.0,
            "borrow": 2_000.0,
            "leverage": 3.0,
            "paper": False,
            "tx_hash": opening_hash,
            "execution_id": execution_id,
        },
        {
            "type": "cycle",
            "ts": "2026-01-01T01:00:00Z",
            "signal": "hold",
            "decision": "hold",
        },
        {
            "type": "trade",
            "action": "close",
            "ts": "2026-01-01T02:00:00Z",
            "asset": "WETH",
            "direction": "long",
            "position_id": "WETH/USDC",
            "entry_price": 2_000.0,
            "close_price": 2_010.0,
            "supply": 1.0,
            "borrow": 2_000.0,
            "leverage": 3.0,
            "realised_usd": 30.0,
            "paper": False,
            "tx_hash": closing_hash,
        },
        {
            "type": "cycle",
            "ts": "2026-01-01T03:00:00Z",
            "signal": "hold",
            "decision": "hold",
        },
    ]
    journal = [
        {
            "execution_id": execution_id,
            "action": "open",
            "status": "complete",
            "tx_hash": opening_hash,
            "steps": [{"tx_hash": opening_hash, "receipt": {"status": 1}}],
            "receipt": {"status": 1},
            "state_event": {"type": "trade", "action": "open"},
        }
    ]

    report = build_report(entries, journal)

    assert report["cycles"]["by_category"]["position_management_hold"] == 1
    assert report["cycles"]["by_category"]["flat_signal_hold"] == 1
    assert report["cycles"]["unexplained_cycles"] == 0
    assert report["trades"]["coverage"]["journal_joined"] == 1
    assert report["trades"]["pnl"]["formula_realised_usd"] == 30.0
    assert report["trades"]["pnl"]["reconciled_realised_usd"] is None
    assert report["trades"]["pnl"]["confidence"] == "unproven"
    assert any(
        finding["code"] == "trade_missing_execution_id"
        for finding in report["trades"]["findings"]
    )


def test_audit_since_timestamp_limits_cycle_report() -> None:
    entries = [
        {
            "type": "cycle",
            "ts": "2026-01-01T00:00:00Z",
            "signal": "hold",
            "decision": "hold",
        },
        {
            "type": "cycle",
            "ts": "2026-01-01T01:00:00Z",
            "signal": "skip",
            "decision": "skip_volatility",
        },
    ]

    report = build_report(entries, [], since="2026-01-01T01:00:00Z")

    assert report["cycles"]["total_cycles"] == 1
    assert report["cycles"]["by_category"] == {"strategy_gate": 1}


def test_read_only_journal_loader_does_not_create_missing_file(tmp_path) -> None:
    path = tmp_path / "missing.sqlite3"

    assert load_journal_records(str(path)) == []
    assert not path.exists()


def test_audit_report_is_json_serializable() -> None:
    report = build_report([], [])

    json.dumps(report)


def test_rpc_enrichment_is_read_only_and_decodes_wallet_transfer(monkeypatch) -> None:
    tx_hash = _hash("c")
    wallet = "0x00000000000000000000000000000000000000aa"
    usdc = audit._ASSET_ADDR["USDC"]
    transfer_topic = "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex()
    padded_wallet = "0x" + ("0" * 24) + wallet[2:]
    padded_other = "0x" + ("0" * 24) + ("bb" * 20)
    calls: list[str] = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(_url, *, json, timeout):
        del timeout
        calls.append(json["method"])
        if json["method"] == "eth_chainId":
            return Response({"result": "0x2105"})
        if json["method"] == "eth_getTransactionReceipt":
            return Response(
                {
                    "result": {
                        "status": "0x1",
                        "blockNumber": "0x10",
                        "gasUsed": "0x2",
                        "effectiveGasPrice": "0x3",
                        "logs": [
                            {
                                "address": usdc,
                                "topics": [
                                    transfer_topic,
                                    padded_other,
                                    padded_wallet,
                                ],
                                "data": "0xf4240",
                            }
                        ],
                    }
                }
            )
        if json["method"] == "eth_getTransactionByHash":
            return Response(
                {
                    "result": {
                        "from": wallet,
                        "to": usdc,
                        "nonce": "0x1",
                        "blockNumber": "0x10",
                    }
                }
            )
        raise AssertionError(json["method"])

    monkeypatch.setattr(audit.httpx, "post", fake_post)
    report = build_report(
        [
            {
                "type": "trade",
                "action": "open",
                "asset": "WETH",
                "direction": "long",
                "entry_price": 2_000,
                "supply": 1,
                "borrow": 2_000,
                "leverage": 3,
                "paper": False,
                "tx_hash": tx_hash,
            }
        ],
        [],
    )

    enriched = audit.enrich_with_rpc(report, ReadOnlyRpc("https://rpc.test"), wallet)

    assert calls == [
        "eth_chainId",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
    ]
    transaction = enriched["trades"]["transactions"][0]
    assert transaction["rpc_receipt"]["gas_wei"] == 6
    assert transaction["transfers"][0]["token"] == "USDC"
    assert transaction["transfers"][0]["wallet_delta"] == 1.0
    assert enriched["sources"]["rpc"]["wallet_token_deltas"]["USDC"] == 1.0
    assert "rpc.test" not in json.dumps(enriched)


def test_rpc_reader_rejects_mutating_method_without_network() -> None:
    reader = ReadOnlyRpc("https://rpc.test")

    try:
        reader.call("eth_sendRawTransaction", [])
    except ValueError as error:
        assert str(error) == "RPC method is not allowlisted"
    else:
        raise AssertionError("mutating RPC method was accepted")

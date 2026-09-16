from types import SimpleNamespace

import bot.onchain as onchain


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


def test_recent_liquidations_chunks_strict_rpc_ranges(monkeypatch) -> None:
    requests: list[dict] = []

    def fake_post(_url, *, json, timeout):
        del timeout
        if json["method"] == "eth_blockNumber":
            return _Response({"result": "0x64"})
        requests.append(json)
        return _Response({"result": [{}]})

    monkeypatch.setattr(onchain.httpx, "post", fake_post)
    web3 = SimpleNamespace(provider=SimpleNamespace(endpoint_uri="https://rpc.test"))

    result = onchain._recent_liquidations(web3, 21)

    assert result == 3
    assert [
        (request["params"][0]["fromBlock"], request["params"][0]["toBlock"])
        for request in requests
    ] == [("0x50", "0x59"), ("0x5a", "0x63"), ("0x64", "0x64")]

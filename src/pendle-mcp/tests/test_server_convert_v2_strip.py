import copy

import pytest

from pendle_mcp import server


SAMPLE_RESPONSE = {
    "action": "swap",
    "inputs": [{"token": "0xt1", "amount": "1000"}],
    "requiredApprovals": [],
    "routes": [
        {
            "contractParamInfo": {
                "method": "swapExactSyForPt",
                "contractCallParamsName": ["receiver", "market", "minPtOut"],
                "contractCallParams": ["0xreceiver", "0xmarket", "999"],
            },
            "tx": {
                "data": "0xdeadbeef" * 200,
                "to": "0xrouter",
                "from": "0xreceiver",
            },
            "outputs": [{"token": "0xpt", "amount": "1278"}],
            "data": {
                "aggregatorType": "VOID",
                "priceImpact": -0.003,
                "priceImpactBreakDown": {
                    "internalPriceImpact": -0.003,
                    "externalPriceImpact": 0,
                },
                "fee": {"usd": 2.35},
            },
        }
    ],
}


def test_strip_drops_tx_and_contract_call_params() -> None:
    response = copy.deepcopy(SAMPLE_RESPONSE)
    stripped = server._strip_convert_v2_tx_fields(response)

    route = stripped["routes"][0]
    assert "tx" not in route
    assert "contractCallParams" not in route["contractParamInfo"]
    # Kept fields
    assert route["contractParamInfo"]["method"] == "swapExactSyForPt"
    assert route["contractParamInfo"]["contractCallParamsName"] == [
        "receiver",
        "market",
        "minPtOut",
    ]
    assert route["outputs"] == [{"token": "0xpt", "amount": "1278"}]
    assert route["data"]["aggregatorType"] == "VOID"
    assert route["data"]["priceImpactBreakDown"]["internalPriceImpact"] == -0.003
    assert route["data"]["fee"]["usd"] == 2.35
    # Top-level kept
    assert stripped["action"] == "swap"
    assert stripped["inputs"] == [{"token": "0xt1", "amount": "1000"}]


def test_strip_is_noop_for_non_dict_or_missing_routes() -> None:
    assert server._strip_convert_v2_tx_fields(None) is None
    assert server._strip_convert_v2_tx_fields([1, 2, 3]) == [1, 2, 3]
    assert server._strip_convert_v2_tx_fields({"action": "swap"}) == {"action": "swap"}


class _DummyClient:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def convert_v2(self, **_kwargs):
        self.calls += 1
        return copy.deepcopy(self._response)


def _patch_from_env(monkeypatch: pytest.MonkeyPatch, response) -> _DummyClient:
    dummy = _DummyClient(response)

    def fake_from_env(cls):
        return dummy

    monkeypatch.setattr(server.PendleApiClient, "from_env", classmethod(fake_from_env))
    return dummy


@pytest.mark.asyncio
async def test_pendle_convert_v2_default_strips_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_from_env(monkeypatch, SAMPLE_RESPONSE)

    result = await server.pendle_convert_v2(
        chain_id=1,
        slippage=0.005,
        tokens_in=["0xt1"],
        amounts_in=["1000"],
        tokens_out=["0xpt"],
    )

    route = result["routes"][0]
    assert "tx" not in route
    assert "contractCallParams" not in route["contractParamInfo"]


@pytest.mark.asyncio
async def test_pendle_convert_v2_include_tx_preserves_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_from_env(monkeypatch, SAMPLE_RESPONSE)

    result = await server.pendle_convert_v2(
        chain_id=1,
        slippage=0.005,
        tokens_in=["0xt1"],
        amounts_in=["1000"],
        tokens_out=["0xpt"],
        include_tx=True,
    )

    route = result["routes"][0]
    assert route["tx"]["to"] == "0xrouter"
    assert route["tx"]["data"].startswith("0xdeadbeef")
    assert route["contractParamInfo"]["contractCallParams"] == [
        "0xreceiver",
        "0xmarket",
        "999",
    ]

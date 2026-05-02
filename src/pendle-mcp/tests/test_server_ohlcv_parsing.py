import pytest

from pendle_mcp import server


class _DummyClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_prices_ohlcv_v4(self, **_kwargs):
        return self._response


def _patch_from_env(monkeypatch: pytest.MonkeyPatch, response) -> None:
    def fake_from_env(cls):
        return _DummyClient(response)

    monkeypatch.setattr(server.PendleApiClient, "from_env", classmethod(fake_from_env))


@pytest.mark.asyncio
async def test_pendle_get_prices_ohlcv_v4_parse_results_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "results": (
            "time,open,high,low,close,volume\n"
            "2026-01-01T00:00:00Z,1,2,3,4,5\n"
            "\n"
            "2026-01-01T01:00:00Z,6,7,8,9,10\n"
        )
    }
    _patch_from_env(monkeypatch, response)

    data = await server.pendle_get_prices_ohlcv_v4(
        chain_id=1,
        address="0xasset",
        parse_results=True,
    )

    assert data["results"] == response["results"]
    assert data["parse_error"] is None
    assert data["results_parsed"] == [
        {
            "time": "2026-01-01T00:00:00Z",
            "open": "1",
            "high": "2",
            "low": "3",
            "close": "4",
            "volume": "5",
        },
        {
            "time": "2026-01-01T01:00:00Z",
            "open": "6",
            "high": "7",
            "low": "8",
            "close": "9",
            "volume": "10",
        },
    ]


@pytest.mark.asyncio
async def test_pendle_get_prices_ohlcv_v4_parse_results_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {
        "results": "time,open,high,low,close,volume\n2026-01-01T00:00:00Z,1,2,3,4\n"
    }
    _patch_from_env(monkeypatch, response)

    data = await server.pendle_get_prices_ohlcv_v4(
        chain_id=1,
        address="0xasset",
        parse_results=True,
    )

    assert data["results_parsed"] is None
    assert data["parse_error"].startswith("ValueError:")


@pytest.mark.asyncio
async def test_pendle_get_prices_ohlcv_v4_parse_results_missing_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {"candles": []}
    _patch_from_env(monkeypatch, response)

    data = await server.pendle_get_prices_ohlcv_v4(
        chain_id=1,
        address="0xasset",
        parse_results=True,
    )

    assert data["results_parsed"] is None
    assert data["parse_error"] == "Response missing 'results' CSV string."


@pytest.mark.asyncio
async def test_pendle_get_prices_ohlcv_v4_default_does_not_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {"results": "time,open,high,low,close,volume\n2026-01-01T00:00:00Z,1,2,3,4,5\n"}
    _patch_from_env(monkeypatch, response)

    data = await server.pendle_get_prices_ohlcv_v4(chain_id=1, address="0xasset")

    assert data == response


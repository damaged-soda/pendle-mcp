import pytest

from pendle_mcp import server


def _make_row(
    *,
    action: str,
    tx_hash: str,
    chain_id: int = 1,
    market: str = "0xmarket",
    timestamp: str = "2026-05-01T00:00:00.000Z",
    profit_usd: float = 0.0,
    profit_asset: float = 0.0,
    profit_eth: float = 0.0,
    yt_spent_usd: float = 0.0,
    pt_spent_usd: float = 0.0,
    lp_spent_usd: float = 0.0,
    tx_value_asset: float = 0.0,
) -> dict:
    return {
        "chainId": chain_id,
        "market": market,
        "user": "0xuser",
        "timestamp": timestamp,
        "action": action,
        "txHash": tx_hash,
        "ptData": {"unit": 0, "spent_v2": {"usd": pt_spent_usd, "asset": 0, "eth": 0}},
        "ytData": {"unit": 0, "spent_v2": {"usd": yt_spent_usd, "asset": 0, "eth": 0}},
        "lpData": {"unit": 0, "spent_v2": {"usd": lp_spent_usd, "asset": 0, "eth": 0}},
        "profit": {"usd": profit_usd, "asset": profit_asset, "eth": profit_eth},
        "txValueAsset": tx_value_asset,
    }


class _PaginatedClient:
    """Pretends to be PendleApiClient. Returns slices of `rows` per call,
    bounded by `skip` and `limit`. Records each call for assertions."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_user_pnl_transactions(self, **kwargs):
        self.calls.append(kwargs)
        skip = kwargs.get("skip") or 0
        limit = kwargs.get("limit") or len(self._rows)
        return {"total": len(self._rows), "results": self._rows[skip : skip + limit]}


def _patch_client(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setattr(
        server.PendleApiClient,
        "from_env",
        classmethod(lambda cls: client),
    )


# --- pendle_get_user_pnl_transactions: pure paginator (regression) -----------


@pytest.mark.asyncio
async def test_paginator_passes_through_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_make_row(action="buyPt", tx_hash="0x1")]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_transactions(user="0xuser")

    assert data == {"total": 1, "results": rows}
    assert client.calls == [
        {
            "user": "0xuser",
            "skip": None,
            "limit": None,
            "chain_id": None,
            "market": None,
        }
    ]


# --- pendle_get_user_pnl_summary --------------------------------------------


@pytest.mark.asyncio
async def test_summary_aggregates_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _make_row(
            action="buyPt",
            tx_hash="0xa",
            profit_usd=10,
            pt_spent_usd=100,
            tx_value_asset=200,
        ),
        _make_row(
            action="buyPt",
            tx_hash="0xb",
            profit_usd=5,
            pt_spent_usd=50,
            tx_value_asset=100,
            chain_id=42161,
            market="0xother",
        ),
        _make_row(
            action="redeemYtYield",
            tx_hash="0xc",
            profit_usd=20,
            yt_spent_usd=80,
        ),
    ]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_summary(
        user="0xuser",
        page_size=2,  # forces 2 pages
    )

    assert data["groupBy"] == "action"
    assert data["scanned"] == 3
    assert data["pagesFetched"] == 2
    assert "total" not in data, "summary must not surface API page total"
    assert "truncated" not in data, "summary tool refuses to truncate"
    # paginated 2 + 1
    assert [c["skip"] for c in client.calls] == [0, 2]
    assert [c["limit"] for c in client.calls] == [2, 2]

    by_key = {g["key"]: g for g in data["groups"]}
    assert set(by_key) == {"buyPt", "redeemYtYield"}
    buy_pt = by_key["buyPt"]
    assert buy_pt["count"] == 2
    assert buy_pt["profitUsd"] == 15
    assert buy_pt["spentUsd"] == 150
    assert buy_pt["txValueAsset"] == 300
    assert buy_pt["chainIds"] == [1, 42161]
    assert buy_pt["markets"] == ["0xmarket", "0xother"]
    assert data["groups"][0]["key"] == "buyPt"  # sorted by count desc


@pytest.mark.asyncio
async def test_summary_emits_top_level_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _make_row(
            action="buyPt",
            tx_hash="0xa",
            profit_usd=10,
            profit_asset=0.5,
            profit_eth=0.001,
            tx_value_asset=200,
        ),
        _make_row(
            action="buyPt",
            tx_hash="0xb",
            profit_usd=-3,
            profit_asset=-0.1,
            profit_eth=-0.0002,
            tx_value_asset=80,
        ),
        _make_row(
            action="redeemYtYield",
            tx_hash="0xc",
            profit_usd=20,
            profit_asset=1.0,
            profit_eth=0.002,
            tx_value_asset=0,
        ),
    ]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_summary(user="0xuser")

    assert data["totalProfitUsd"] == pytest.approx(27.0)
    assert data["totalProfitAsset"] == pytest.approx(1.4)
    assert data["totalProfitEth"] == pytest.approx(0.0028)
    assert data["totalTxValueAsset"] == pytest.approx(280.0)
    # Sanity: totals == sum across groups
    assert sum(g["profitUsd"] for g in data["groups"]) == pytest.approx(
        data["totalProfitUsd"]
    )


@pytest.mark.asyncio
async def test_summary_group_by_tx_hash_collapses_multi_action_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _make_row(
            action="addLiquidity",
            tx_hash="0xzap",
            timestamp="2026-05-01T00:00:00.000Z",
            profit_usd=1,
            lp_spent_usd=500,
        ),
        _make_row(
            action="buyPt",
            tx_hash="0xzap",
            timestamp="2026-04-30T23:59:59.000Z",  # earlier
            profit_usd=2,
            pt_spent_usd=300,
        ),
        _make_row(
            action="buyPt",
            tx_hash="0xother",
            profit_usd=3,
            pt_spent_usd=100,
        ),
    ]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_summary(
        user="0xuser",
        group_by="tx_hash",
    )

    assert data["scanned"] == 3
    by_key = {g["key"]: g for g in data["groups"]}
    zap = by_key["0xzap"]
    assert zap["count"] == 2
    assert zap["actions"] == ["addLiquidity", "buyPt"]
    assert zap["spentUsd"] == 800
    assert zap["profitUsd"] == 3
    assert zap["timestamp"] == "2026-04-30T23:59:59.000Z"
    assert zap["chainId"] == 1
    assert zap["market"] == "0xmarket"


@pytest.mark.asyncio
async def test_summary_short_page_stops_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_make_row(action="buyPt", tx_hash=f"0x{i}") for i in range(3)]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_summary(
        user="0xuser",
        page_size=10,
    )

    # one call: returned 3 rows < page_size, loop stops.
    assert data["pagesFetched"] == 1
    assert data["scanned"] == 3


@pytest.mark.asyncio
async def test_summary_invalid_group_by(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _PaginatedClient([])
    _patch_client(monkeypatch, client)

    with pytest.raises(ValueError, match="group_by"):
        await server.pendle_get_user_pnl_summary(
            user="0xuser",
            group_by="market",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_summary_invalid_page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _PaginatedClient([])
    _patch_client(monkeypatch, client)

    with pytest.raises(ValueError, match="page_size"):
        await server.pendle_get_user_pnl_summary(
            user="0xuser",
            page_size=0,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_summary_hard_cap_raises_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Lower the cap so the test stays small. Each page returns 100 rows; 2 pages
    # = 200 rows > cap=150 → must raise.
    monkeypatch.setattr(server, "_PNL_SUMMARY_HARD_CAP_ROWS", 150)
    rows = [_make_row(action="buyPt", tx_hash=f"0x{i}") for i in range(500)]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    with pytest.raises(ValueError, match="more than 150 PnL rows"):
        await server.pendle_get_user_pnl_summary(user="0xuser")
    # Loop must stop the moment cap is exceeded — should not have scanned all 500.
    assert sum(c["limit"] for c in client.calls) <= 200

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
        "profit": {"usd": profit_usd, "asset": 0, "eth": 0},
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


@pytest.mark.asyncio
async def test_group_by_none_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _PaginatedClient([_make_row(action="buyPt", tx_hash="0x1")])
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_transactions(user="0xuser")

    assert data == {"total": 1, "results": client._rows}
    assert client.calls == [
        {
            "user": "0xuser",
            "skip": None,
            "limit": None,
            "chain_id": None,
            "market": None,
        }
    ]


@pytest.mark.asyncio
async def test_group_by_action_aggregates_across_pages(
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

    data = await server.pendle_get_user_pnl_transactions(
        user="0xuser",
        group_by="action",
        limit=2,  # forces 2 pages
    )

    assert data["groupBy"] == "action"
    assert data["total"] == 3
    assert data["scanned"] == 3
    assert data["pagesFetched"] == 2
    assert data["truncated"] is False
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
    # buyPt count > redeem count, sorted desc
    assert data["groups"][0]["key"] == "buyPt"


@pytest.mark.asyncio
async def test_group_by_tx_hash_collapses_multi_action_tx(
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

    data = await server.pendle_get_user_pnl_transactions(
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
    # earliest timestamp wins
    assert zap["timestamp"] == "2026-04-30T23:59:59.000Z"
    assert zap["chainId"] == 1
    assert zap["market"] == "0xmarket"


@pytest.mark.asyncio
async def test_group_by_max_pages_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_make_row(action="buyPt", tx_hash=f"0x{i}") for i in range(10)]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_transactions(
        user="0xuser",
        group_by="action",
        limit=3,
        max_pages=2,
    )

    assert data["pagesFetched"] == 2
    assert data["scanned"] == 6  # 3 + 3
    assert data["total"] == 10
    assert data["truncated"] is True
    assert data["groups"][0]["count"] == 6


@pytest.mark.asyncio
async def test_group_by_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _PaginatedClient([])
    _patch_client(monkeypatch, client)

    with pytest.raises(ValueError, match="group_by"):
        await server.pendle_get_user_pnl_transactions(
            user="0xuser",
            group_by="market",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_group_by_rejects_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _PaginatedClient([])
    _patch_client(monkeypatch, client)

    with pytest.raises(ValueError, match="skip"):
        await server.pendle_get_user_pnl_transactions(
            user="0xuser",
            group_by="action",
            skip=10,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_group_by_short_page_stops_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_make_row(action="buyPt", tx_hash=f"0x{i}") for i in range(3)]
    client = _PaginatedClient(rows)
    _patch_client(monkeypatch, client)

    data = await server.pendle_get_user_pnl_transactions(
        user="0xuser",
        group_by="action",
        limit=10,
    )

    # one call: returned 3 rows < page_size, loop stops.
    assert data["pagesFetched"] == 1
    assert data["scanned"] == 3
    assert data["truncated"] is False

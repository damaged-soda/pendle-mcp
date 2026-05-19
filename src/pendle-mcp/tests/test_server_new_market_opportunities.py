from __future__ import annotations

import datetime as _real_dt
from typing import Any, Mapping

import pytest

from pendle_mcp import server
from pendle_mcp.chain_apy import ChainTruthResult

_REAL_DATETIME = _real_dt.datetime


def _market(
    *,
    name: str,
    address: str,
    timestamp: str,
    tvl: float,
    underlying_apy: float,
    implied_apy: float,
    sy: str,
    expiry: str = "2026-08-06T00:00:00.000Z",
) -> dict[str, Any]:
    return {
        "name": name,
        "address": address,
        "timestamp": timestamp,
        "expiry": expiry,
        "sy": sy,
        "isNew": True,
        "isPrime": False,
        "isVolatile": False,
        "categoryIds": ["stable"],
        "details": {
            "totalTvl": tvl,
            "underlyingApy": underlying_apy,
            "impliedApy": implied_apy,
        },
    }


class _FakeClient:
    def __init__(self, markets: list[Mapping[str, Any]]) -> None:
        self._markets = markets

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get_markets_all(
        self,
        *,
        chain_id: int | None = None,
        is_active: bool | None = None,
        skip: int | None = None,
        limit: int | None = None,
        **__: Any,
    ) -> dict[str, Any]:
        assert chain_id == 1
        assert is_active is True
        start = skip or 0
        end = start + (limit or 100)
        return {
            "total": len(self._markets),
            "skip": start,
            "limit": limit,
            "results": self._markets[start:end],
        }


@pytest.mark.asyncio
async def test_detect_new_market_opportunities_flags_implied_discount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markets = [
        _market(
            name="savUSD",
            address="0x05b5a8bd55d17a7cc8133d8ccd285f83be01a062",
            timestamp="2026-05-08T01:47:35.000Z",
            tvl=3_263_808,
            underlying_apy=0.078,
            implied_apy=0.066,
            sy="1-0x0caefeb807152fbd945bf947893e6feba5aed61b",
        ),
        _market(
            name="oldETH",
            address="0x1111111111111111111111111111111111111111",
            timestamp="2025-01-01T00:00:00.000Z",
            tvl=10_000_000,
            underlying_apy=0.01,
            implied_apy=0.01,
            sy="1-0x1111111111111111111111111111111111111111",
        ),
    ]

    class FakeClientFactory:
        @staticmethod
        def from_env() -> _FakeClient:
            return _FakeClient(markets)

    async def fake_compute(
        *, chain_id: int, market: Mapping[str, Any], window_days: int
    ) -> ChainTruthResult:
        assert chain_id == 1
        assert market["sy"] == "1-0x0caefeb807152fbd945bf947893e6feba5aed61b"
        assert window_days == 90
        return ChainTruthResult.ok(
            value=0.074,
            method="sy_accumulator",
            confidence="high",
            window_days=window_days,
        )

    monkeypatch.setattr(server, "PendleApiClient", FakeClientFactory)
    monkeypatch.setattr(server, "compute_chain_truth_for_market", fake_compute)
    monkeypatch.setattr(
        server.dt,
        "datetime",
        _FixedDateTime,
    )

    result = await server._detect_new_market_opportunities(
        chain_id=1,
        market_age_days=30,
        chain_truth_window_days=90,
        spread_threshold_bps=200,
        implied_discount_threshold_bps=50,
        min_tvl_usd=500_000,
        include_non_opportunities=True,
        calibration_concurrency=1,
    )

    assert result["summary"]["markets_scanned"] == 2
    assert result["summary"]["prefilter_candidates"] == 1
    assert result["summary"]["opportunities"] == 1
    opportunity = result["opportunities"][0]
    assert opportunity["market_name"] == "savUSD"
    assert opportunity["ui_spread_bps"] == pytest.approx(-40.0)
    assert opportunity["implied_discount_bps"] == pytest.approx(80.0)
    assert opportunity["chain_truth_status"] == "ok"
    assert opportunity["chain_truth_method"] == "sy_accumulator"
    assert opportunity["trigger_reasons"] == ["market_implied_below_chain_truth"]
    assert result["prefilter_skipped"][0]["reason"] == "market_too_old"


@pytest.mark.asyncio
async def test_detect_new_market_opportunities_surfaces_chain_truth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markets = [
        _market(
            name="broken",
            address="0x2222222222222222222222222222222222222222",
            timestamp="2026-05-10T00:00:00.000Z",
            tvl=1_000_000,
            underlying_apy=0.01,
            implied_apy=0.01,
            sy="1-0x2222222222222222222222222222222222222222",
        )
    ]

    class FakeClientFactory:
        @staticmethod
        def from_env() -> _FakeClient:
            return _FakeClient(markets)

    async def fake_compute(**kwargs: Any) -> ChainTruthResult:
        return ChainTruthResult.fail(
            status="contract_revert",
            method="sy_accumulator",
            error="eth_call returned empty data",
            window_days=kwargs.get("window_days"),
        )

    monkeypatch.setattr(server, "PendleApiClient", FakeClientFactory)
    monkeypatch.setattr(server, "compute_chain_truth_for_market", fake_compute)
    monkeypatch.setattr(server.dt, "datetime", _FixedDateTime)

    result = await server._detect_new_market_opportunities(
        chain_id=1,
        market_age_days=30,
        chain_truth_window_days=90,
        spread_threshold_bps=200,
        implied_discount_threshold_bps=50,
        min_tvl_usd=500_000,
        include_non_opportunities=True,
        calibration_concurrency=1,
    )

    assert result["summary"]["opportunities"] == 0
    assert result["summary"]["chain_truth_errors"] == 1
    assert result["summary"]["unknown_candidates"] == 1
    assert result["unknown_candidates"][0]["chain_truth_status"] == "contract_revert"
    assert result["non_opportunities"][0]["chain_truth_error"] == "eth_call returned empty data"


class _FixedDateTime:
    @classmethod
    def now(cls, tz: Any = None) -> Any:
        return _REAL_DATETIME(2026, 5, 18, 14, 0, tzinfo=tz)

    @classmethod
    def fromisoformat(cls, value: str) -> Any:
        return _REAL_DATETIME.fromisoformat(value)

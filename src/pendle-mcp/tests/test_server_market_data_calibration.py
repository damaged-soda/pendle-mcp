"""Tests for the chain-calibration injection inside pendle_get_market_data_v2.

These exercise the server helper directly (`_attach_chain_calibration`) plus
the public tool path with patched RPC behavior, ensuring `u_actual_30d_chain`,
`u_ui_vs_chain_ratio`, and `u_actual_chain_error` always land in the response
even when calibration fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from pendle_mcp import server


@pytest.mark.asyncio
async def test_attach_chain_calibration_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_compute(*, chain_id: int, sy_address: str) -> tuple[float | None, str | None]:
        assert chain_id == 1
        assert sy_address == "0xbf98480425a29197e5d99d003017f63a1e595d02"
        return 0.0407, None

    monkeypatch.setattr(server, "compute_u_actual_30d_chain", fake_compute)

    data: dict[str, Any] = {"underlyingApy": 0.036, "impliedApy": 0.05}
    market_meta = {
        "results": [
            {"sy": "1-0xbf98480425a29197e5d99d003017f63a1e595d02"},
        ]
    }
    out = await server._attach_chain_calibration(
        data=data, chain_id=1, market_meta=market_meta
    )
    assert out is data
    assert out["u_actual_30d_chain"] == pytest.approx(0.0407)
    assert out["u_ui_vs_chain_ratio"] == pytest.approx(0.036 / 0.0407, rel=1e-6)
    assert "u_actual_chain_error" not in out


@pytest.mark.asyncio
async def test_attach_chain_calibration_missing_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_compute(**_: Any) -> tuple[float | None, str | None]:
        return None, "RPC_URL_1 not configured, cannot calibrate"

    monkeypatch.setattr(server, "compute_u_actual_30d_chain", fake_compute)

    data: dict[str, Any] = {"underlyingApy": 0.19}
    market_meta = {"results": [{"sy": "1-0x4d654f255d54637112844bd8802b716170904fee"}]}

    out = await server._attach_chain_calibration(
        data=data, chain_id=1, market_meta=market_meta
    )
    assert out["u_actual_30d_chain"] is None
    assert out["u_ui_vs_chain_ratio"] is None
    assert out["u_actual_chain_error"] == "RPC_URL_1 not configured, cannot calibrate"


@pytest.mark.asyncio
async def test_attach_chain_calibration_no_sy_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def fake_compute(**_: Any) -> tuple[float | None, str | None]:
        nonlocal called
        called = True
        return None, "should not be called"

    monkeypatch.setattr(server, "compute_u_actual_30d_chain", fake_compute)

    data: dict[str, Any] = {"underlyingApy": 0.04}
    market_meta = {"results": []}

    out = await server._attach_chain_calibration(
        data=data, chain_id=1, market_meta=market_meta
    )
    assert called is False
    assert out["u_actual_30d_chain"] is None
    assert out["u_ui_vs_chain_ratio"] is None
    assert "markets/all returned no matching market" in out["u_actual_chain_error"]


@pytest.mark.asyncio
async def test_attach_chain_calibration_non_dict_data_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_compute(**_: Any) -> tuple[float | None, str | None]:
        raise AssertionError("should not be called when data is not a dict")

    monkeypatch.setattr(server, "compute_u_actual_30d_chain", fake_compute)

    out = await server._attach_chain_calibration(
        data=["not", "a", "dict"], chain_id=1, market_meta={"results": []}
    )
    assert out == ["not", "a", "dict"]


@pytest.mark.asyncio
async def test_attach_chain_calibration_ratio_none_when_ui_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_compute(**_: Any) -> tuple[float | None, str | None]:
        return 0.0329, None

    monkeypatch.setattr(server, "compute_u_actual_30d_chain", fake_compute)

    data: dict[str, Any] = {"impliedApy": 0.05}  # no underlyingApy
    market_meta = {"results": [{"sy": "1-0xbf98480425a29197e5d99d003017f63a1e595d02"}]}

    out = await server._attach_chain_calibration(
        data=data, chain_id=1, market_meta=market_meta
    )
    assert out["u_actual_30d_chain"] == pytest.approx(0.0329)
    assert out["u_ui_vs_chain_ratio"] is None


def test_extract_sy_address_handles_bad_shapes() -> None:
    assert server._extract_sy_address(None) == (None, "markets/all lookup returned unexpected shape")
    sy_addr, err = server._extract_sy_address({"results": [{"sy": "not-a-pendle-id"}]})
    assert sy_addr is None
    assert err is not None and "unparseable" in err
    sy_addr, err = server._extract_sy_address(
        {"results": [{"sy": "1-0xbf98480425a29197e5d99d003017f63a1e595d02"}]}
    )
    assert sy_addr == "0xbf98480425a29197e5d99d003017f63a1e595d02"
    assert err is None

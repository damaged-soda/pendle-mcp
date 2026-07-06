import io
import json

import pytest

from pendle_mcp import cli, server
from pendle_mcp.pendle_api import PendleApiError


class _DummyClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def convert_v2(self, **_kwargs):
        return self._response


def _patch_from_env(monkeypatch: pytest.MonkeyPatch, response) -> None:
    def fake_from_env(cls):
        return _DummyClient(response)

    monkeypatch.setattr(server.PendleApiClient, "from_env", classmethod(fake_from_env))


def test_discover_tools_finds_known_tool() -> None:
    tools = cli._discover_tools()
    assert "pendle_convert_v2" in tools
    assert "pendle_health" in tools
    # Helpers stay out
    assert "_strip_convert_v2_tx_fields" not in tools


def test_list_prints_sorted_tool_names(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line]
    assert lines == sorted(lines)
    assert "pendle_convert_v2" in lines


def test_show_includes_signature_and_docstring(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.main(["show", "pendle_convert_v2"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pendle_convert_v2" in out
    assert "include_tx" in out
    assert "chain_id" in out
    assert "Universal convert function" in out


def test_show_unknown_tool_returns_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["show", "pendle_does_not_exist"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown tool" in err


def test_call_unknown_tool_returns_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["call", "pendle_does_not_exist", "--json", "{}"])
    assert rc == 2


def test_call_invalid_json_returns_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["call", "pendle_convert_v2", "--json", "not-json"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid JSON args" in err


def test_call_json_must_be_object(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["call", "pendle_convert_v2", "--json", "[1,2,3]"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "must be a JSON object" in err


def test_call_dispatches_kwargs_and_prints_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {
        "action": "swap",
        "inputs": [],
        "requiredApprovals": [],
        "routes": [
            {
                "contractParamInfo": {
                    "method": "swapExactSyForPt",
                    "contractCallParamsName": [],
                    "contractCallParams": [],
                },
                "tx": {"data": "0xdead", "to": "0xrouter", "from": "0xsender"},
                "outputs": [{"token": "0xpt", "amount": "1000"}],
                "data": {"aggregatorType": "VOID"},
            }
        ],
    }
    _patch_from_env(monkeypatch, response)

    args = {
        "chain_id": 1,
        "slippage": 0.005,
        "tokens_in": ["0xt1"],
        "amounts_in": ["1000"],
        "tokens_out": ["0xpt"],
    }
    rc = cli.main(["call", "pendle_convert_v2", "--json", json.dumps(args)])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    # Default include_tx=False — tx must be gone
    route = parsed["routes"][0]
    assert "tx" not in route
    assert "contractCallParams" not in route["contractParamInfo"]


def test_call_reads_json_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {"routes": []}
    _patch_from_env(monkeypatch, response)

    args = {
        "chain_id": 1,
        "slippage": 0.005,
        "tokens_in": ["0xt1"],
        "amounts_in": ["1000"],
        "tokens_out": ["0xpt"],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(args)))
    rc = cli.main(["call", "pendle_convert_v2", "--json", "-"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"routes": []}


def test_call_renders_pendle_api_error_to_stderr_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def convert_v2(self, **_kwargs):
            raise PendleApiError(
                "boom",
                error_type="server_error",
                status_code=500,
                method="GET",
                path="/v2/sdk/1/convert",
                attempts=2,
                retries_exhausted=True,
                detail="upstream blew up",
            )

    def fake_from_env(cls):
        return _RaisingClient()

    monkeypatch.setattr(server.PendleApiClient, "from_env", classmethod(fake_from_env))

    args = {
        "chain_id": 1,
        "slippage": 0.005,
        "tokens_in": ["0xt1"],
        "amounts_in": ["1000"],
        "tokens_out": ["0xpt"],
    }
    rc = cli.main(["call", "pendle_convert_v2", "--json", json.dumps(args)])
    assert rc == 1
    err = capsys.readouterr().err
    parsed = json.loads(err)
    assert parsed["error"] == "PendleApiError"
    assert parsed["error_type"] == "server_error"
    assert parsed["status_code"] == 500
    assert parsed["retries_exhausted"] is True
    assert parsed["detail"] == "upstream blew up"


def test_call_argument_error_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {"routes": []}
    _patch_from_env(monkeypatch, response)

    # Missing required chain_id
    rc = cli.main(["call", "pendle_convert_v2", "--json", "{}"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "argument error" in err


# ---------------------------------------------------------------------------
# native subcommand surface (one subcommand per MCP tool)
# ---------------------------------------------------------------------------


def _tool_name_to_subcommand(tool_name: str) -> str:
    return tool_name.removeprefix("pendle_").replace("_", "-")


def test_every_mcp_tool_has_a_native_subcommand() -> None:
    parser = cli._build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, __import__("argparse")._SubParsersAction)
    )
    subcommands = set(subparsers_action.choices)
    for tool_name in cli._discover_tools():
        assert _tool_name_to_subcommand(tool_name) in subcommands, (
            f"MCP tool {tool_name} has no matching CLI subcommand"
        )


class _RecordingClient:
    """Records kwargs of the first client method call and returns a response."""

    calls: dict[str, dict] = {}

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def __getattr__(self, name):
        async def method(**kwargs):
            _RecordingClient.calls[name] = kwargs
            return self._response

        return method


def _patch_recording_client(monkeypatch: pytest.MonkeyPatch, response) -> None:
    _RecordingClient.calls = {}

    def fake_from_env(cls):
        return _RecordingClient(response)

    monkeypatch.setattr(server.PendleApiClient, "from_env", classmethod(fake_from_env))


def test_get_chains_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_recording_client(monkeypatch, [1, 10, 8453])
    rc = cli.main(["get-chains"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [1, 10, 8453]


def test_get_markets_all_maps_flags_and_json_ids(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_recording_client(monkeypatch, {"total": 0, "results": []})
    rc = cli.main(
        [
            "get-markets-all",
            "--chain-id", "1",
            "--ids", '["1-0xabc"]',
            "--is-active",
            "--order-by", "totalTvl:-1",
            "--skip", "0",
            "--limit", "5",
        ]
    )
    assert rc == 0
    kwargs = _RecordingClient.calls["get_markets_all"]
    assert kwargs == {
        "chain_id": 1,
        "ids": ["1-0xabc"],
        "is_active": True,
        "order_by": "totalTvl:-1",
        "skip": 0,
        "limit": 5,
    }
    assert json.loads(capsys.readouterr().out) == {"total": 0, "results": []}


def test_tristate_flag_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recording_client(monkeypatch, {"results": []})
    rc = cli.main(["get-markets-points-market"])
    assert rc == 0
    kwargs = _RecordingClient.calls["get_markets_points_market"]
    assert kwargs["is_active"] is None


def test_tristate_flag_negation(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recording_client(monkeypatch, {"results": []})
    rc = cli.main(["get-markets-points-market", "--no-is-active"])
    assert rc == 0
    kwargs = _RecordingClient.calls["get_markets_points_market"]
    assert kwargs["is_active"] is False


def test_json_array_flag_rejects_invalid_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["get-markets-all", "--ids", "not-json"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--ids" in err


def test_json_array_flag_rejects_non_string_items(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["get-markets-all", "--ids", "[1, 2]"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "JSON array of strings" in err


def test_asset_type_flag_converts_to_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_recording_client(monkeypatch, {"results": []})
    rc = cli.main(["get-assets-all", "--chain-id", "1", "--asset-type", "PT"])
    assert rc == 0
    kwargs = _RecordingClient.calls["get_assets_all"]
    from pendle_mcp.pendle_api import PendleAssetType

    assert kwargs["asset_type"] is PendleAssetType.PT


def test_convert_v2_subcommand_strips_tx_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {
        "routes": [
            {
                "contractParamInfo": {
                    "method": "swapExactSyForPt",
                    "contractCallParamsName": [],
                    "contractCallParams": [],
                },
                "tx": {"data": "0xdead"},
                "outputs": [],
                "data": {"aggregatorType": "VOID"},
            }
        ],
    }
    _patch_recording_client(monkeypatch, response)
    rc = cli.main(
        [
            "convert-v2",
            "--chain-id", "1",
            "--slippage", "0.005",
            "--tokens-in", '["0xt1"]',
            "--amounts-in", '["1000"]',
            "--tokens-out", '["0xpt"]',
        ]
    )
    assert rc == 0
    kwargs = _RecordingClient.calls["convert_v2"]
    assert kwargs["tokens_in"] == ["0xt1"]
    assert kwargs["amounts_in"] == ["1000"]
    route = json.loads(capsys.readouterr().out)["routes"][0]
    assert "tx" not in route
    assert "contractCallParams" not in route["contractParamInfo"]


def test_subcommand_renders_pendle_api_error_and_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get_chains(self, **_kwargs):
            raise PendleApiError(
                "boom",
                error_type="server_error",
                status_code=500,
                method="GET",
                path="/v1/chains",
            )

    monkeypatch.setattr(
        server.PendleApiClient,
        "from_env",
        classmethod(lambda cls: _RaisingClient()),
    )
    rc = cli.main(["get-chains"])
    assert rc == 1
    parsed = json.loads(capsys.readouterr().err)
    assert parsed["error"] == "PendleApiError"
    assert parsed["status_code"] == 500


def test_subcommand_value_error_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(
        ["detect-new-market-opportunities", "--market-age-days", "-1"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "market_age_days" in err

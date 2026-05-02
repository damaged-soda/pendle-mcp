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

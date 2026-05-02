"""CLI for invoking pendle-mcp tools directly without the MCP client.

Useful for live-testing tool changes against the real Pendle API without
restarting Claude Code (or any other MCP host) to reload the stdio server.
The CLI imports the same async tool functions the MCP server exposes, so it
exercises the full tool path: argument validation, API client, response
post-processing — everything except the FastMCP transport layer itself.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from typing import Any, Callable

from pendle_mcp import server
from pendle_mcp.pendle_api import PendleApiError


def _discover_tools() -> dict[str, Callable[..., Any]]:
    return {
        name: obj
        for name, obj in vars(server).items()
        if name.startswith("pendle_") and inspect.iscoroutinefunction(obj)
    }


def _format_annotation(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return "Any"
    return inspect.formatannotation(annotation)


def _format_signature(func: Callable[..., Any]) -> str:
    sig = inspect.signature(func)
    lines: list[str] = []
    for param in sig.parameters.values():
        if param.kind != inspect.Parameter.KEYWORD_ONLY:
            continue
        ann = _format_annotation(param.annotation)
        default = (
            "" if param.default is inspect.Parameter.empty else f" = {param.default!r}"
        )
        lines.append(f"  {param.name}: {ann}{default}")
    return "\n".join(lines) if lines else "  (no parameters)"


def _read_json_args(args_json: str | None) -> dict[str, Any]:
    if args_json is None or args_json == "-":
        text = sys.stdin.read()
    else:
        text = args_json
    if not text.strip():
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"args must be a JSON object (got {type(parsed).__name__})"
        )
    return parsed


def _format_pendle_api_error(err: PendleApiError) -> dict[str, Any]:
    return {
        "error": "PendleApiError",
        "message": str(err),
        "error_type": err.error_type,
        "status_code": err.status_code,
        "method": err.method,
        "path": err.path,
        "params": dict(err.params) if err.params else None,
        "attempts": err.attempts,
        "retries_exhausted": err.retries_exhausted,
        "url": err.url,
        "detail": err.detail,
    }


def cmd_list(_args: argparse.Namespace) -> int:
    for name in sorted(_discover_tools()):
        print(name)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    tools = _discover_tools()
    func = tools.get(args.tool)
    if func is None:
        print(f"unknown tool: {args.tool}", file=sys.stderr)
        return 2
    print(args.tool)
    if func.__doc__:
        print()
        print(inspect.cleandoc(func.__doc__))
    print()
    print("parameters (all keyword-only):")
    print(_format_signature(func))
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    tools = _discover_tools()
    func = tools.get(args.tool)
    if func is None:
        print(f"unknown tool: {args.tool}", file=sys.stderr)
        return 2
    try:
        kwargs = _read_json_args(args.json)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"invalid JSON args: {e}", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(func(**kwargs))
    except PendleApiError as e:
        print(
            json.dumps(_format_pendle_api_error(e), indent=2, default=str),
            file=sys.stderr,
        )
        return 1
    except TypeError as e:
        print(f"argument error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pendle-mcp-cli",
        description=(
            "Invoke pendle-mcp tools directly (live-testing without restarting "
            "the MCP host)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List all available tools.")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show a tool's signature and docstring.")
    p_show.add_argument("tool")
    p_show.set_defaults(func=cmd_show)

    p_call = sub.add_parser("call", help="Call a tool with JSON kwargs.")
    p_call.add_argument("tool")
    p_call.add_argument(
        "--json",
        help="JSON object of kwargs. Use '-' or omit to read from stdin.",
        default=None,
    )
    p_call.set_defaults(func=cmd_call)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

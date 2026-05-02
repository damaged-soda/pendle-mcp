from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import os
import random
import re
from enum import Enum
from types import TracebackType
from typing import Any, Mapping

import httpx

DEFAULT_PENDLE_API_BASE_URL = "https://api-v2.pendle.finance/core"
DEFAULT_PENDLE_API_TIMEOUT_SECONDS = 20.0
DEFAULT_PENDLE_API_MAX_RETRIES = 1
DEFAULT_PENDLE_API_RETRY_BACKOFF_SECONDS = 0.2
DEFAULT_PENDLE_API_RETRY_JITTER_RATIO = 0.1
DEFAULT_PENDLE_API_ERROR_DETAIL_MAX_CHARS = 2048


class PendleAssetType(str, Enum):
    PENDLE_LP = "PENDLE_LP"
    SY = "SY"
    PT = "PT"
    YT = "YT"


class TransactionType(str, Enum):
    TRADES = "TRADES"
    LIQUIDITY = "LIQUIDITY"


class TransactionAction(str, Enum):
    LONG_YIELD = "LONG_YIELD"
    SHORT_YIELD = "SHORT_YIELD"
    ADD_LIQUIDITY = "ADD_LIQUIDITY"
    REMOVE_LIQUIDITY = "REMOVE_LIQUIDITY"


class PendleApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        status_code: int | None = None,
        method: str | None = None,
        path: str | None = None,
        params: Mapping[str, str] | None = None,
        url: str | None = None,
        detail: str | None = None,
        attempts: int | None = None,
        retries_exhausted: bool | None = None,
        detail_max_chars: int = DEFAULT_PENDLE_API_ERROR_DETAIL_MAX_CHARS,
    ) -> None:
        self.error_type = error_type
        self.status_code = status_code
        self.method = method
        self.path = path
        self.params = params
        self.url = url
        self.detail = detail
        self.attempts = attempts
        self.retries_exhausted = retries_exhausted
        self._detail_max_chars = detail_max_chars
        super().__init__(self._format_message(message))

    def _format_message(self, message: str) -> str:
        parts: list[str] = []
        if self.error_type:
            parts.append(f"error_type={self.error_type}")
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.method:
            parts.append(f"method={self.method}")
        if self.path:
            parts.append(f"path={self.path}")
        if self.params:
            parts.append(f"params={dict(self.params)}")
        if self.attempts is not None:
            parts.append(f"attempts={self.attempts}")
        if self.retries_exhausted is not None:
            parts.append(f"retries_exhausted={self.retries_exhausted}")
        if self.url:
            parts.append(f"url={self.url}")
        if self.detail:
            detail = self.detail.strip()
            if len(detail) > self._detail_max_chars:
                detail = detail[: self._detail_max_chars] + "…(truncated)"
            parts.append(f"detail={detail}")
        if not parts:
            return message
        return f"{message} ({', '.join(parts)})"

    def summary(self, *, max_chars: int = 240) -> str:
        message = str(self)
        if len(message) <= max_chars:
            return message
        return message[:max_chars] + "…(truncated)"


def _encode_ids(ids: list[str] | None) -> str | None:
    if not ids:
        return None
    return ",".join(ids)


def _encode_csv(values: list[str] | None) -> str | None:
    if not values:
        return None
    return ",".join(values)


def _encode_bool(value: bool) -> str:
    return "true" if value else "false"


_BASE10_UINT_RE = re.compile(r"^[0-9]+$")


def _validate_amounts_in(amounts_in: list[str]) -> None:
    for i, amount in enumerate(amounts_in):
        if not isinstance(amount, str) or not _BASE10_UINT_RE.fullmatch(amount):
            raise ValueError(
                "amounts_in must be base-10 integer strings in the input token's smallest unit "
                f"(e.g. wei). Invalid amounts_in[{i}]={amount!r}. "
                "Do not pass decimals like '0.001'. Example (decimals=18): 0.001 => '1000000000000000'."
            )


def _validate_slippage(slippage: float) -> None:
    if slippage < 0 or slippage > 1:
        raise ValueError(
            "slippage must be a fraction between 0 and 1 (e.g. 0.5% -> 0.005; 50% -> 0.5). "
            f"Invalid slippage={slippage!r}."
        )


def _validate_convert_lists(
    *, tokens_in: list[str], amounts_in: list[str], tokens_out: list[str]
) -> None:
    if not tokens_in:
        raise ValueError("tokens_in must not be empty")
    if not tokens_out:
        raise ValueError("tokens_out must not be empty")
    if len(tokens_in) != len(amounts_in):
        raise ValueError(
            "tokens_in and amounts_in must have the same length. "
            f"Got len(tokens_in)={len(tokens_in)} and len(amounts_in)={len(amounts_in)}."
        )


_TIME_FRAME_ALIASES: dict[str, str] = {"1h": "hour", "1d": "day", "1w": "week"}
_TIME_FRAME_ALLOWED: set[str] = {"hour", "day", "week"}


def _normalize_time_frame(time_frame: str | None) -> str | None:
    if time_frame is None:
        return None
    value = time_frame.strip().lower()
    if not value:
        raise ValueError(
            "time_frame must be one of hour/day/week (aliases: 1h/1d/1w). Got empty string."
        )
    normalized = _TIME_FRAME_ALIASES.get(value, value)
    if normalized in _TIME_FRAME_ALLOWED:
        return normalized
    raise ValueError(
        "time_frame must be one of hour/day/week (aliases: 1h->hour, 1d->day, 1w->week). "
        f"Invalid time_frame={time_frame!r}. Example: time_frame='1d' will be normalized to 'day'."
    )


def _sanitize_param_value(key: str, value: Any) -> str:
    text = str(value)
    if key in {"additionalData"}:
        if text.startswith("0x") and len(text) > 18:
            return f"{text[:10]}…{text[-4:]}(len={len(text)})"
        return "…(redacted)"
    if len(text) > 256:
        return text[:256] + "…(truncated)"
    return text


def _sanitize_params(params: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not params:
        return None
    sanitized: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        sanitized[key] = _sanitize_param_value(key, value)
    return sanitized or None


def _sanitize_url(url: httpx.URL) -> str:
    params = dict(url.params)
    if not params:
        return str(url)
    sanitized_params = {key: _sanitize_param_value(key, value) for key, value in params.items()}
    return str(url.copy_with(params=sanitized_params))


def _parse_retry_after(retry_after: str | None) -> float | None:
    if retry_after is None:
        return None
    value = retry_after.strip()
    if not value:
        return None
    if value.isdigit():
        return float(int(value))
    try:
        return float(value)
    except ValueError:
        pass
    try:
        date = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    delta = (date - now).total_seconds()
    return max(delta, 0.0)


class PendleApiErrorType(str, Enum):
    CLIENT_ERROR = "client_error"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    NETWORK_ERROR = "network_error"
    INVALID_JSON = "invalid_json"


def _validate_relative_path(path: str) -> None:
    if not path.startswith("/"):
        raise ValueError("path must start with '/'")
    if "://" in path:
        raise ValueError("path must be a relative path, not a full URL")
    if ".." in path:
        raise ValueError("path must not contain '..'")


class PendleApiClient:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_PENDLE_API_BASE_URL,
        timeout_seconds: float = DEFAULT_PENDLE_API_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_PENDLE_API_MAX_RETRIES,
        retry_backoff_seconds: float = DEFAULT_PENDLE_API_RETRY_BACKOFF_SECONDS,
        retry_jitter_ratio: float = DEFAULT_PENDLE_API_RETRY_JITTER_RATIO,
        error_detail_max_chars: int = DEFAULT_PENDLE_API_ERROR_DETAIL_MAX_CHARS,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("Specify only one of http_client or transport")

        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be >= 0")
        if retry_jitter_ratio < 0:
            raise ValueError("retry_jitter_ratio must be >= 0")
        if error_detail_max_chars <= 0:
            raise ValueError("error_detail_max_chars must be > 0")

        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_jitter_ratio = retry_jitter_ratio
        self._error_detail_max_chars = error_detail_max_chars

    @classmethod
    def from_env(cls) -> "PendleApiClient":
        base_url = os.getenv("PENDLE_API_BASE_URL", DEFAULT_PENDLE_API_BASE_URL).strip()

        timeout_raw = os.getenv(
            "PENDLE_API_TIMEOUT_SECONDS", str(DEFAULT_PENDLE_API_TIMEOUT_SECONDS)
        ).strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as e:
            raise ValueError(
                "PENDLE_API_TIMEOUT_SECONDS must be a number (seconds)"
            ) from e

        max_retries_raw = os.getenv(
            "PENDLE_API_MAX_RETRIES", str(DEFAULT_PENDLE_API_MAX_RETRIES)
        ).strip()
        try:
            max_retries = int(max_retries_raw)
        except ValueError as e:
            raise ValueError("PENDLE_API_MAX_RETRIES must be an integer") from e

        retry_backoff_raw = os.getenv(
            "PENDLE_API_RETRY_BACKOFF_SECONDS",
            str(DEFAULT_PENDLE_API_RETRY_BACKOFF_SECONDS),
        ).strip()
        try:
            retry_backoff_seconds = float(retry_backoff_raw)
        except ValueError as e:
            raise ValueError(
                "PENDLE_API_RETRY_BACKOFF_SECONDS must be a number (seconds)"
            ) from e

        detail_max_chars_raw = os.getenv(
            "PENDLE_API_ERROR_DETAIL_MAX_CHARS",
            str(DEFAULT_PENDLE_API_ERROR_DETAIL_MAX_CHARS),
        ).strip()
        try:
            error_detail_max_chars = int(detail_max_chars_raw)
        except ValueError as e:
            raise ValueError("PENDLE_API_ERROR_DETAIL_MAX_CHARS must be an integer") from e
        if error_detail_max_chars <= 0:
            raise ValueError("PENDLE_API_ERROR_DETAIL_MAX_CHARS must be > 0")

        return cls(
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            error_detail_max_chars=error_detail_max_chars,
        )

    def _get_retry_sleep_seconds(
        self, attempt: int, *, retry_after_seconds: float | None = None
    ) -> float:
        base = (
            max(retry_after_seconds, 0.0)
            if retry_after_seconds is not None
            else self._retry_backoff_seconds * (2**attempt)
        )
        if base <= 0 or self._retry_jitter_ratio <= 0:
            return max(base, 0.0)
        jitter = random.random() * base * self._retry_jitter_ratio
        return base + jitter

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "PendleApiClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def get_json(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Any:
        _validate_relative_path(path)
        sanitized_params = _sanitize_params(params)

        for attempt in range(self._max_retries + 1):
            attempts = attempt + 1
            try:
                resp = await self._client.get(path, params=params)
            except httpx.RequestError as e:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._get_retry_sleep_seconds(attempt))
                    continue
                raise PendleApiError(
                    "Pendle API request failed",
                    error_type=PendleApiErrorType.NETWORK_ERROR.value,
                    method="GET",
                    path=path,
                    params=sanitized_params,
                    url=_sanitize_url(e.request.url),
                    detail=str(e),
                    attempts=attempts,
                    retries_exhausted=True,
                    detail_max_chars=self._error_detail_max_chars,
                ) from e

            if resp.status_code < 200 or resp.status_code >= 300:
                if resp.status_code == 429:
                    error_type = PendleApiErrorType.RATE_LIMITED
                elif resp.status_code >= 500:
                    error_type = PendleApiErrorType.UPSTREAM_ERROR
                else:
                    error_type = PendleApiErrorType.CLIENT_ERROR

                should_retry = attempt < self._max_retries and error_type in {
                    PendleApiErrorType.RATE_LIMITED,
                    PendleApiErrorType.UPSTREAM_ERROR,
                }
                if should_retry:
                    retry_after_seconds = (
                        _parse_retry_after(resp.headers.get("Retry-After"))
                        if error_type == PendleApiErrorType.RATE_LIMITED
                        else None
                    )
                    await asyncio.sleep(
                        self._get_retry_sleep_seconds(
                            attempt, retry_after_seconds=retry_after_seconds
                        )
                    )
                    continue

                raise PendleApiError(
                    "Pendle API returned non-2xx response",
                    error_type=error_type.value,
                    status_code=resp.status_code,
                    method=resp.request.method,
                    path=path,
                    params=sanitized_params,
                    url=_sanitize_url(resp.url),
                    detail=resp.text,
                    attempts=attempts,
                    retries_exhausted=(
                        error_type
                        in {
                            PendleApiErrorType.RATE_LIMITED,
                            PendleApiErrorType.UPSTREAM_ERROR,
                        }
                        and attempt >= self._max_retries
                    ),
                    detail_max_chars=self._error_detail_max_chars,
                )

            try:
                return resp.json()
            except ValueError as e:
                raise PendleApiError(
                    "Pendle API returned invalid JSON",
                    error_type=PendleApiErrorType.INVALID_JSON.value,
                    status_code=resp.status_code,
                    method=resp.request.method,
                    path=path,
                    params=sanitized_params,
                    url=_sanitize_url(resp.url),
                    detail=resp.text,
                    attempts=attempts,
                    retries_exhausted=False,
                    detail_max_chars=self._error_detail_max_chars,
                ) from e

        raise RuntimeError("unreachable")

    async def get_chains(self) -> Any:
        return await self.get_json("/v1/chains")

    async def get_markets_all(
        self,
        *,
        chain_id: int | None = None,
        ids: list[str] | None = None,
        is_active: bool | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if is_active is not None:
            params["isActive"] = _encode_bool(is_active)
        if chain_id is not None:
            params["chainId"] = chain_id
        if ids:
            params["ids"] = _encode_ids(ids)
        return await self.get_json("/v1/markets/all", params=params or None)

    async def get_markets_points_market(
        self,
        *,
        chain_id: int | None = None,
        is_active: bool | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if is_active is not None:
            params["isActive"] = _encode_bool(is_active)
        if chain_id is not None:
            params["chainId"] = chain_id
        return await self.get_json("/v1/markets/points-market", params=params or None)

    async def get_market_data_v2(
        self,
        *,
        chain_id: int,
        address: str,
        timestamp: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if timestamp is not None:
            params["timestamp"] = timestamp
        return await self.get_json(
            f"/v2/{chain_id}/markets/{address}/data",
            params=params or None,
        )

    async def get_market_historical_data_v2(
        self,
        *,
        chain_id: int,
        address: str,
        time_frame: str | None = None,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
        fields: list[str] | None = None,
        include_fee_breakdown: bool | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        normalized_time_frame = _normalize_time_frame(time_frame)
        if normalized_time_frame is not None:
            params["time_frame"] = normalized_time_frame
        if timestamp_start is not None:
            params["timestamp_start"] = timestamp_start
        if timestamp_end is not None:
            params["timestamp_end"] = timestamp_end
        if fields:
            params["fields"] = _encode_csv(fields)
        if include_fee_breakdown is not None:
            params["includeFeeBreakdown"] = _encode_bool(include_fee_breakdown)
        return await self.get_json(
            f"/v2/{chain_id}/markets/{address}/historical-data",
            params=params or None,
        )

    async def get_assets_all(
        self,
        *,
        ids: list[str] | None = None,
        chain_id: int | None = None,
        skip: int | None = None,
        limit: int | None = None,
        asset_type: PendleAssetType | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if ids:
            params["ids"] = _encode_ids(ids)
        if chain_id is not None:
            params["chainId"] = chain_id
        if skip is not None:
            params["skip"] = skip
        if limit is not None:
            params["limit"] = limit
        if asset_type is not None:
            params["type"] = asset_type.value
        return await self.get_json("/v1/assets/all", params=params or None)

    async def get_asset_prices(
        self,
        *,
        ids: list[str] | None = None,
        chain_id: int | None = None,
        skip: int | None = None,
        limit: int | None = None,
        asset_type: PendleAssetType | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if ids:
            params["ids"] = _encode_ids(ids)
        if chain_id is not None:
            params["chainId"] = chain_id
        if skip is not None:
            params["skip"] = skip
        if limit is not None:
            params["limit"] = limit
        if asset_type is not None:
            params["type"] = asset_type.value
        return await self.get_json("/v1/prices/assets", params=params or None)

    async def get_prices_ohlcv_v4(
        self,
        *,
        chain_id: int,
        address: str,
        time_frame: str | None = None,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        normalized_time_frame = _normalize_time_frame(time_frame)
        if normalized_time_frame is not None:
            params["time_frame"] = normalized_time_frame
        if timestamp_start is not None:
            params["timestamp_start"] = timestamp_start
        if timestamp_end is not None:
            params["timestamp_end"] = timestamp_end
        return await self.get_json(
            f"/v4/{chain_id}/prices/{address}/ohlcv",
            params=params or None,
        )

    async def get_user_pnl_transactions(
        self,
        *,
        user: str,
        skip: int | None = None,
        limit: int | None = None,
        chain_id: int | None = None,
        market: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {"user": user}
        if skip is not None:
            params["skip"] = skip
        if limit is not None:
            params["limit"] = limit
        if chain_id is not None:
            params["chainId"] = chain_id
        if market is not None:
            params["market"] = market
        return await self.get_json("/v1/pnl/transactions", params=params)

    async def get_market_transactions_v5(
        self,
        *,
        chain_id: int,
        address: str,
        transaction_type: TransactionType | None = None,
        min_value: float | None = None,
        tx_origin: str | None = None,
        action: TransactionAction | None = None,
        resume_token: str | None = None,
        limit: int | None = None,
        skip: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if transaction_type is not None:
            params["type"] = transaction_type.value
        if min_value is not None:
            params["minValue"] = min_value
        if tx_origin is not None:
            params["txOrigin"] = tx_origin
        if action is not None:
            params["action"] = action.value
        if resume_token is not None:
            params["resumeToken"] = resume_token
        if limit is not None:
            params["limit"] = limit
        if skip is not None:
            params["skip"] = skip
        return await self.get_json(
            f"/v5/{chain_id}/transactions/{address}",
            params=params or None,
        )

    async def get_user_positions(
        self,
        *,
        user: str,
        filter_usd: float | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if filter_usd is not None:
            params["filterUsd"] = filter_usd
        return await self.get_json(
            f"/v1/dashboard/positions/database/{user}",
            params=params or None,
        )

    async def get_merkle_claimed_rewards(
        self,
        *,
        user: str,
    ) -> Any:
        return await self.get_json(f"/v1/dashboard/merkle-claimed-rewards/{user}")

    async def get_limit_orders_all_v2(
        self,
        *,
        chain_id: int | None = None,
        limit: int | None = None,
        maker: str | None = None,
        yt: str | None = None,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
        resume_token: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if chain_id is not None:
            params["chainId"] = chain_id
        if limit is not None:
            params["limit"] = limit
        if maker is not None:
            params["maker"] = maker
        if yt is not None:
            params["yt"] = yt
        if timestamp_start is not None:
            params["timestamp_start"] = timestamp_start
        if timestamp_end is not None:
            params["timestamp_end"] = timestamp_end
        if resume_token is not None:
            params["resumeToken"] = resume_token
        return await self.get_json("/v2/limit-orders", params=params or None)

    async def get_limit_orders_archived_v2(
        self,
        *,
        chain_id: int | None = None,
        limit: int | None = None,
        maker: str | None = None,
        yt: str | None = None,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
        resume_token: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if chain_id is not None:
            params["chainId"] = chain_id
        if limit is not None:
            params["limit"] = limit
        if maker is not None:
            params["maker"] = maker
        if yt is not None:
            params["yt"] = yt
        if timestamp_start is not None:
            params["timestamp_start"] = timestamp_start
        if timestamp_end is not None:
            params["timestamp_end"] = timestamp_end
        if resume_token is not None:
            params["resumeToken"] = resume_token
        return await self.get_json("/v2/limit-orders/archived", params=params or None)

    async def get_limit_orders_book_v2(
        self,
        *,
        chain_id: int,
        precision_decimal: int,
        market: str,
        limit: int | None = None,
        include_amm: bool | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "precisionDecimal": precision_decimal,
            "market": market,
        }
        if limit is not None:
            params["limit"] = limit
        if include_amm is not None:
            params["includeAmm"] = _encode_bool(include_amm)
        return await self.get_json(f"/v2/limit-orders/book/{chain_id}", params=params)

    async def get_limit_orders_maker_limit_orders(
        self,
        *,
        chain_id: int,
        maker: str,
        skip: int | None = None,
        limit: int | None = None,
        yt: str | None = None,
        order_type: int | None = None,
        is_active: bool | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "chainId": chain_id,
            "maker": maker,
        }
        if skip is not None:
            params["skip"] = skip
        if limit is not None:
            params["limit"] = limit
        if yt is not None:
            params["yt"] = yt
        if order_type is not None:
            params["type"] = order_type
        if is_active is not None:
            params["isActive"] = _encode_bool(is_active)
        return await self.get_json("/v1/limit-orders/makers/limit-orders", params=params)

    async def get_limit_orders_taker_limit_orders(
        self,
        *,
        chain_id: int,
        yt: str,
        order_type: int,
        skip: int | None = None,
        limit: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {
            "chainId": chain_id,
            "yt": yt,
            "type": order_type,
        }
        if skip is not None:
            params["skip"] = skip
        if limit is not None:
            params["limit"] = limit
        if sort_by is not None:
            params["sortBy"] = sort_by
        if sort_order is not None:
            params["sortOrder"] = sort_order
        return await self.get_json("/v1/limit-orders/takers/limit-orders", params=params)

    async def get_supported_aggregators(self, *, chain_id: int) -> Any:
        return await self.get_json(f"/v1/sdk/{chain_id}/supported-aggregators")

    async def get_market_tokens(self, *, chain_id: int, market: str) -> Any:
        return await self.get_json(f"/v1/sdk/{chain_id}/markets/{market}/tokens")

    async def get_swapping_prices(self, *, chain_id: int, market: str) -> Any:
        return await self.get_json(f"/v1/sdk/{chain_id}/markets/{market}/swapping-prices")

    async def get_pt_cross_chain_metadata(self, *, chain_id: int, pt: str) -> Any:
        return await self.get_json(f"/v1/sdk/{chain_id}/cross-chain-pt-metadata/{pt}")

    async def convert_v2(
        self,
        *,
        chain_id: int,
        slippage: float,
        tokens_in: list[str],
        amounts_in: list[str],
        tokens_out: list[str],
        receiver: str | None = None,
        enable_aggregator: bool | None = None,
        aggregators: list[str] | None = None,
        redeem_rewards: bool | None = None,
        need_scale: bool | None = None,
        additional_data: str | None = None,
        use_limit_order: bool | None = None,
    ) -> Any:
        _validate_slippage(slippage)
        _validate_convert_lists(
            tokens_in=tokens_in,
            amounts_in=amounts_in,
            tokens_out=tokens_out,
        )
        _validate_amounts_in(amounts_in)
        params: dict[str, Any] = {
            "slippage": slippage,
            "tokensIn": _encode_csv(tokens_in),
            "amountsIn": _encode_csv(amounts_in),
            "tokensOut": _encode_csv(tokens_out),
        }
        if receiver is not None:
            params["receiver"] = receiver
        if enable_aggregator is not None:
            params["enableAggregator"] = _encode_bool(enable_aggregator)
        if aggregators:
            params["aggregators"] = _encode_csv(aggregators)
        if redeem_rewards is not None:
            params["redeemRewards"] = _encode_bool(redeem_rewards)
        if need_scale is not None:
            params["needScale"] = _encode_bool(need_scale)
        if additional_data is not None:
            params["additionalData"] = additional_data
        if use_limit_order is not None:
            params["useLimitOrder"] = _encode_bool(use_limit_order)

        return await self.get_json(f"/v2/sdk/{chain_id}/convert", params=params)

    async def get_ve_pendle_data_v2(self) -> Any:
        return await self.get_json("/v2/ve-pendle/data")

    async def get_ve_pendle_market_fees_chart(
        self,
        *,
        timestamp_start: str | None = None,
        timestamp_end: str | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if timestamp_start is not None:
            params["timestamp_start"] = timestamp_start
        if timestamp_end is not None:
            params["timestamp_end"] = timestamp_end
        return await self.get_json(
            "/v1/ve-pendle/market-fees-chart",
            params=params or None,
        )

    async def get_distinct_user_from_token(
        self,
        *,
        token: str,
        chain_id: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {"token": token}
        if chain_id is not None:
            params["chainId"] = chain_id
        return await self.get_json(
            "/v1/statistics/get-distinct-user-from-token",
            params=params,
        )

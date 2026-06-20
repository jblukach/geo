import ipaddress
import json
import os
import time
from typing import Any
from urllib.parse import parse_qs, unquote


IP_INPUT_KEYS = ("ip", "ipAddress", "query")
MAX_IPS_PER_REQUEST_ENV = "MAX_IPS_PER_REQUEST"
MAX_IPS_PER_REQUEST_DEFAULT = 300
MAX_REQUEST_BODY_BYTES_ENV = "MAX_REQUEST_BODY_BYTES"
MAX_REQUEST_BODY_BYTES_DEFAULT = 262144
MIN_REMAINING_TIME_MS_ENV = "MIN_REMAINING_TIME_MS"
MIN_REMAINING_TIME_MS_DEFAULT = 1500
VALKEY_PORT_DEFAULT = 6379
VALKEY_TLS_DEFAULT = True
VALKEY_ASN_V4_SET_NAME_DEFAULT = "asn_v4_ranges"
VALKEY_ASN_V6_SET_NAME_DEFAULT = "asn_v6_ranges"
VALKEY_CITY_V4_SET_NAME_DEFAULT = "city_v4_ranges"
VALKEY_CITY_V6_SET_NAME_DEFAULT = "city_v6_ranges"
VALKEY_LAST_UPDATED_ASN_KEY_DEFAULT = "geo:last_updated:asn"
VALKEY_LAST_UPDATED_CITY_KEY_DEFAULT = "geo:last_updated:city"
ATTRIBUTION_TEXT = "This product includes GeoLite2 data created by MaxMind, available from https://www.maxmind.com."


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _valkey_port() -> int:
    raw_value = os.environ.get("VALKEY_PORT", str(VALKEY_PORT_DEFAULT)).strip()
    if not raw_value:
        return VALKEY_PORT_DEFAULT
    return int(raw_value)


def _valkey_tls_enabled() -> bool:
    raw_value = os.environ.get("VALKEY_TLS", str(VALKEY_TLS_DEFAULT)).strip()
    if not raw_value:
        return VALKEY_TLS_DEFAULT
    return _is_truthy(raw_value)


def _max_ips_per_request() -> int:
    raw_value = os.environ.get(MAX_IPS_PER_REQUEST_ENV, str(MAX_IPS_PER_REQUEST_DEFAULT)).strip()
    if not raw_value:
        return MAX_IPS_PER_REQUEST_DEFAULT
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {MAX_IPS_PER_REQUEST_ENV}: {raw_value}") from exc
    if value <= 0:
        raise RuntimeError(f"{MAX_IPS_PER_REQUEST_ENV} must be greater than zero")
    return value


def _max_request_body_bytes() -> int:
    raw_value = os.environ.get(MAX_REQUEST_BODY_BYTES_ENV, str(MAX_REQUEST_BODY_BYTES_DEFAULT)).strip()
    if not raw_value:
        return MAX_REQUEST_BODY_BYTES_DEFAULT
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {MAX_REQUEST_BODY_BYTES_ENV}: {raw_value}") from exc
    if value <= 0:
        raise RuntimeError(f"{MAX_REQUEST_BODY_BYTES_ENV} must be greater than zero")
    return value


def _min_remaining_time_ms() -> int:
    raw_value = os.environ.get(MIN_REMAINING_TIME_MS_ENV, str(MIN_REMAINING_TIME_MS_DEFAULT)).strip()
    if not raw_value:
        return MIN_REMAINING_TIME_MS_DEFAULT
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {MIN_REMAINING_TIME_MS_ENV}: {raw_value}") from exc
    if value <= 0:
        raise RuntimeError(f"{MIN_REMAINING_TIME_MS_ENV} must be greater than zero")
    return value


def _has_processing_budget(context: Any, min_remaining_ms: int) -> bool:
    if context is None:
        return True

    remaining_time_fn = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(remaining_time_fn):
        return True

    try:
        remaining_ms = int(remaining_time_fn())
    except (TypeError, ValueError):
        return True

    return remaining_ms >= min_remaining_ms


def _log_event(event_name: str, **fields: Any) -> None:
    payload = {"event": event_name}
    payload.update(fields)
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _append_ip_values(values: list[str], candidate: Any) -> None:
    if candidate is None:
        return
    if isinstance(candidate, str):
        parts = [part.strip() for part in candidate.split(",") if part.strip()]
        values.extend(parts)
        return
    if isinstance(candidate, list):
        for item in candidate:
            _append_ip_values(values, item)


def _input_ips(event: dict[str, Any]) -> list[str]:
    values: list[str] = []

    # API Gateway path input, e.g. /geo/134.129.111.111
    path_parameters = event.get("pathParameters") or {}
    path_parameter_supplied = False
    if isinstance(path_parameters, dict):
        if path_parameters.get("ip") is not None or path_parameters.get("proxy") is not None:
            path_parameter_supplied = True
        _append_ip_values(values, path_parameters.get("ip"))
        _append_ip_values(values, path_parameters.get("proxy"))

    raw_path = event.get("rawPath")
    if not path_parameter_supplied and isinstance(raw_path, str) and raw_path.strip():
        path = raw_path.strip().rstrip("/")
        if "/geo/" in path:
            # API Gateway keeps encoded path text, so decode before parsing as IP.
            _append_ip_values(values, unquote(path.rsplit("/", 1)[-1]))

    raw_query = event.get("rawQueryString")
    raw_query_values: dict[str, list[str]] = {}
    if isinstance(raw_query, str) and raw_query.strip():
        raw_query_values = parse_qs(raw_query, keep_blank_values=False)
        for key in IP_INPUT_KEYS:
            for raw_value in raw_query_values.get(key, []):
                _append_ip_values(values, raw_value)

    multi_value_query_params = event.get("multiValueQueryStringParameters") or {}
    if isinstance(multi_value_query_params, dict):
        for key in IP_INPUT_KEYS:
            _append_ip_values(values, multi_value_query_params.get(key))

    query_params = event.get("queryStringParameters") or {}
    if isinstance(query_params, dict):
        for key in IP_INPUT_KEYS:
            if key in raw_query_values:
                # rawQueryString preserves repeated keys, so prefer it when available.
                continue
            _append_ip_values(values, query_params.get(key))

    for key in IP_INPUT_KEYS:
        _append_ip_values(values, event.get(key))

    _append_ip_values(values, event.get("ips"))

    body = event.get("body")
    if isinstance(body, str) and body.strip():
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in IP_INPUT_KEYS:
                _append_ip_values(values, payload.get(key))
            _append_ip_values(values, payload.get("ips"))
        if isinstance(payload, list):
            _append_ip_values(values, payload)

    if values:
        return values

    # For GET /geo requests without an explicit IP, use the caller source IP.
    request_context = event.get("requestContext") or {}
    if isinstance(request_context, dict):
        http_context = request_context.get("http") or {}
        if isinstance(http_context, dict):
            method = str(http_context.get("method", "")).upper()
            source_ip = http_context.get("sourceIp")
            raw_path = str(event.get("rawPath", "")).rstrip("/")
            if method == "GET" and raw_path == "/geo":
                _append_ip_values(values, source_ip)

    return values


def _set_name(dataset: str, ip_version: int) -> str:
    if dataset == "asn" and ip_version == 4:
        return os.environ.get("VALKEY_ASN_V4_SET_NAME", VALKEY_ASN_V4_SET_NAME_DEFAULT)
    if dataset == "asn" and ip_version == 6:
        return os.environ.get("VALKEY_ASN_V6_SET_NAME", VALKEY_ASN_V6_SET_NAME_DEFAULT)
    if dataset == "city" and ip_version == 4:
        return os.environ.get("VALKEY_CITY_V4_SET_NAME", VALKEY_CITY_V4_SET_NAME_DEFAULT)
    if dataset == "city" and ip_version == 6:
        return os.environ.get("VALKEY_CITY_V6_SET_NAME", VALKEY_CITY_V6_SET_NAME_DEFAULT)
    raise RuntimeError(f"Unsupported dataset/version: {dataset}/{ip_version}")


def _last_updated_key(dataset: str) -> str:
    if dataset == "asn":
        return os.environ.get("VALKEY_LAST_UPDATED_ASN_KEY", VALKEY_LAST_UPDATED_ASN_KEY_DEFAULT)
    if dataset == "city":
        return os.environ.get("VALKEY_LAST_UPDATED_CITY_KEY", VALKEY_LAST_UPDATED_CITY_KEY_DEFAULT)
    raise RuntimeError(f"Unsupported dataset for last-updated key: {dataset}")


def _cache_last_updated(client) -> dict[str, Any]:
    return {
        "asn": client.get(_last_updated_key("asn")),
        "city": client.get(_last_updated_key("city")),
    }


def _parse_member(dataset: str, member: str) -> dict[str, str] | None:
    parts = member.split("|")
    if len(parts) < 4:
        return None

    if dataset == "asn" and len(parts) >= 6:
        return {
            "range_end_int": parts[0],
            "dataset": parts[1],
            "network": parts[2],
            "ip_version": parts[3],
            "asn": parts[4],
            "organization": parts[5],
        }

    if dataset == "city" and len(parts) >= 8:
        return {
            "range_end_int": parts[0],
            "dataset": parts[1],
            "network": parts[2],
            "ip_version": parts[3],
            "country_iso_code": parts[4],
            "country_name": parts[5],
            "subdivision": parts[6],
            "city": parts[7],
        }

    return None


def _lookup_member(client, dataset: str, ip_text: str) -> dict[str, str] | None:
    parsed_ip = ipaddress.ip_address(ip_text)
    ip_int = int(parsed_ip)
    set_name = _set_name(dataset, parsed_ip.version)

    matches = client.zrevrangebyscore(
        set_name,
        ip_int,
        0,
        start=0,
        num=1,
        withscores=False,
    )
    if not matches:
        return None

    member = str(matches[0])
    parsed_member = _parse_member(dataset, member)
    if parsed_member is None:
        return None

    try:
        range_end_int = int(parsed_member["range_end_int"])
    except ValueError:
        return None

    if ip_int > range_end_int:
        return None

    return parsed_member


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, indent=4),
    }


def _error_payload(message: str) -> dict[str, Any]:
    return {
        "error": message,
    }


def _compact_dict(payload: dict[str, Any]) -> dict[str, Any]:
    compacted = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and value == "":
            continue
        compacted[key] = value
    return compacted


def _format_last_updated(raw_value: Any) -> str | None:
    if not raw_value:
        return None

    value = str(raw_value).strip()
    if not value:
        return None

    # Process stores UTC timestamps in ISO-8601 with Z, so return as-is.
    return value


def _build_geo_payload(city_row: dict[str, str] | None) -> dict[str, Any] | None:
    if city_row is None:
        return None

    country_name = str(city_row.get("country_name", "")).strip()
    country_iso = str(city_row.get("country_iso_code", "")).strip()
    if country_name and country_iso:
        country = f"{country_name} - {country_iso}"
    else:
        country = country_name or country_iso or None

    geo = _compact_dict(
        {
            "country": country,
            "state": city_row.get("subdivision"),
            "city": city_row.get("city"),
            "cidr": city_row.get("network"),
        }
    )
    if not geo:
        return None
    return geo


def _build_asn_payload(asn_row: dict[str, str] | None) -> dict[str, Any] | None:
    if asn_row is None:
        return None

    raw_asn = str(asn_row.get("asn", "")).strip()
    if raw_asn.isdigit():
        asn_id: int | str | None = int(raw_asn)
    elif raw_asn:
        asn_id = raw_asn
    else:
        asn_id = None

    asn = _compact_dict(
        {
            "id": asn_id,
            "org": asn_row.get("organization"),
            "net": asn_row.get("network"),
        }
    )
    if not asn:
        return None
    return asn


def _build_result_entry(
    ip_text: str,
    asn_row: dict[str, str] | None,
    city_row: dict[str, str] | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"ip": ip_text}

    geo = _build_geo_payload(city_row)
    if geo is not None:
        entry["geo"] = geo

    asn = _build_asn_payload(asn_row)
    if asn is not None:
        entry["asn"] = asn

    return entry


def _shared_metadata(cache_last_updated: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "attribution": ATTRIBUTION_TEXT,
        "geolite2-asn.csv": _format_last_updated(cache_last_updated.get("asn")),
        "geolite2-city.csv": _format_last_updated(cache_last_updated.get("city")),
    }
    return _compact_dict(metadata)


def handler(event, context):
    start_time = time.perf_counter()
    request_id = getattr(context, "aws_request_id", "") if context is not None else ""
    min_remaining_ms = _min_remaining_time_ms()

    endpoint = os.environ.get("VALKEY_ENDPOINT", "").strip()
    if not endpoint:
        return _response(500, _error_payload("VALKEY_ENDPOINT is required"))

    raw_body = (event or {}).get("body")
    if isinstance(raw_body, str):
        raw_body_bytes = len(raw_body.encode("utf-8"))
        max_body_bytes = _max_request_body_bytes()
        if raw_body_bytes > max_body_bytes:
            return _response(
                413,
                _error_payload(
                    f"Request body too large: {raw_body_bytes} bytes. Maximum allowed is {max_body_bytes}"
                ),
            )

    input_ips = _input_ips(event or {})
    if not input_ips:
        return _response(400, _error_payload("At least one IP address is required"))

    max_ips_per_request = _max_ips_per_request()
    if len(input_ips) > max_ips_per_request:
        return _response(
            400,
            _error_payload(
                f"Too many IPs requested: {len(input_ips)}. Maximum allowed is {max_ips_per_request}"
            ),
        )

    parsed_entries: list[dict[str, Any]] = []
    for raw_ip in input_ips:
        try:
            parsed_entries.append({"ip": str(ipaddress.ip_address(raw_ip)), "valid": True})
        except ValueError:
            parsed_entries.append({"ip": raw_ip, "valid": False})

    valid_count = sum(1 for entry in parsed_entries if entry["valid"])
    unique_valid_ips = {str(entry["ip"]) for entry in parsed_entries if entry["valid"]}

    if not _has_processing_budget(context, min_remaining_ms):
        return _response(
            503,
            _error_payload("Insufficient processing time remaining. Reduce batch size and retry."),
        )

    cache_last_updated = {"asn": None, "city": None}
    lookup_cache: dict[str, tuple[dict[str, str] | None, dict[str, str] | None]] = {}

    if valid_count > 0:
        import redis  # type: ignore[import-not-found]

        client = redis.Redis(
            host=endpoint,
            port=_valkey_port(),
            decode_responses=True,
            ssl=_valkey_tls_enabled(),
        )

        try:
            cache_last_updated = _cache_last_updated(client)
            unique_valid_ips: list[str] = []
            seen: set[str] = set()
            for entry in parsed_entries:
                if not entry["valid"]:
                    continue
                ip_text = str(entry["ip"])
                if ip_text in seen:
                    continue
                seen.add(ip_text)
                unique_valid_ips.append(ip_text)

            for ip_text in unique_valid_ips:
                if not _has_processing_budget(context, min_remaining_ms):
                    return _response(
                        503,
                        _error_payload("Insufficient processing time remaining. Reduce batch size and retry."),
                    )
                asn_result = _lookup_member(client, "asn", ip_text)
                city_result = _lookup_member(client, "city", ip_text)
                lookup_cache[ip_text] = (asn_result, city_result)
        finally:
            client.close()

    results: list[dict[str, Any]] = []
    for entry in parsed_entries:
        ip_text = str(entry["ip"])
        if not entry["valid"]:
            results.append({"ip": ip_text, "error": f"Invalid IP address: {ip_text}"})
            continue

        asn_result, city_result = lookup_cache.get(ip_text, (None, None))
        results.append(_build_result_entry(ip_text, asn_result, city_result))

    metadata = _shared_metadata(cache_last_updated)

    duration_ms = int((time.perf_counter() - start_time) * 1000)
    _log_event(
        "search_request_summary",
        requestId=request_id,
        requestedCount=len(input_ips),
        validCount=valid_count,
        uniqueValidCount=len(unique_valid_ips),
        invalidCount=len(input_ips) - valid_count,
        resultCount=len(results),
        durationMs=duration_ms,
    )

    response_payload: dict[str, Any] = {
        "results": results,
        "requested_count": len(input_ips),
    }

    if "attribution" in metadata:
        response_payload["attribution"] = metadata["attribution"]
    if "geolite2-asn.csv" in metadata:
        response_payload["geolite2-asn.csv"] = metadata["geolite2-asn.csv"]
    if "geolite2-city.csv" in metadata:
        response_payload["geolite2-city.csv"] = metadata["geolite2-city.csv"]

    return _response(200, response_payload)

import csv
import io
import ipaddress
import json
import os
import tempfile
import time
import urllib.parse
from datetime import timedelta
from typing import Any

_S3_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB (S3 minimum part size is 5 MB)


TRIGGER_SOURCE_FILES = {
    "GeoLite2-ASN-Blocks-IPv4.csv",
    "GeoLite2-ASN-Blocks-IPv6.csv",
    "GeoLite2-City-Blocks-IPv4.csv",
    "GeoLite2-City-Blocks-IPv6.csv",
}

JOB_TYPE_SOURCE_BUILD = "source_build"
MOMENTO_SET_PREFIX = "geo"
MOMENTO_SECRET_KEY_DEFAULT = "MOMENTO"
MOMENTO_ENDPOINT_SECRET_KEY_DEFAULT = "MOMENTO_ENDPOINT"
MOMENTO_RELEASE_ENV = "MOMENTO_RELEASE"
MOMENTO_CACHE_NAMES_BY_SOURCE_ENV = "MOMENTO_CACHE_NAMES_BY_SOURCE"
MOMENTO_CACHE_NAME_ENV = "MOMENTO_CACHE_NAME"
MOMENTO_SORTED_SET_BATCH_SIZE_ENV = "MOMENTO_SORTED_SET_BATCH_SIZE"
MOMENTO_SORTED_SET_BATCH_SIZE_DEFAULT = 5000
MOMENTO_SORTED_SET_REQUEST_SIZE_BYTES = 900 * 1024
MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS_ENV = "MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS"
MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS_DEFAULT = 1.0

ASN_OUTPUT_FIELDS = (
    "momento_score",
    "ip_version",
    "shard",
    "momento_set",
    "sort_key",
    "prefix_len",
    "range_start_hex",
    "range_end_hex",
    "network",
    "asn",
    "organization",
)

CITY_OUTPUT_FIELDS = (
    "momento_score",
    "ip_version",
    "shard",
    "momento_set",
    "sort_key",
    "prefix_len",
    "range_start_hex",
    "range_end_hex",
    "network",
    "country_iso_code",
    "country_name",
    "subdivision",
    "city",
)

SOURCE_OUTPUT_CONFIG = {
    "GeoLite2-ASN-Blocks-IPv4.csv": {
        "output": "GeoLite2-ASN-Blocks-IPv4.txt",
        "type": "asn",
    },
    "GeoLite2-ASN-Blocks-IPv6.csv": {
        "output": "GeoLite2-ASN-Blocks-IPv6.txt",
        "type": "asn",
    },
    "GeoLite2-City-Blocks-IPv4.csv": {
        "output": "GeoLite2-City-Blocks-IPv4.txt",
        "type": "city",
    },
    "GeoLite2-City-Blocks-IPv6.csv": {
        "output": "GeoLite2-City-Blocks-IPv6.txt",
        "type": "city",
    },
}

MOMENTO_SOURCE_OUTPUTS = {
    config["output"] for config in SOURCE_OUTPUT_CONFIG.values()
}


_RUNTIME_STATE = {
    "last_processed_source_signatures": {
        key: "" for key in SOURCE_OUTPUT_CONFIG
    },
    "last_momento_request_at": 0.0,
}


def reset_runtime_state() -> None:
    _RUNTIME_STATE["last_processed_source_signatures"] = {
        key: "" for key in SOURCE_OUTPUT_CONFIG
    }
    _RUNTIME_STATE["last_momento_request_at"] = 0.0


def _boto3_client(service_name):
    import boto3  # type: ignore[import-not-found]

    return boto3.client(service_name)


def _log_event(event_name: str, **fields) -> None:
    payload = {"event": event_name}
    payload.update(fields)
    print(json.dumps(payload, sort_keys=True))


def _momento_set_prefix() -> str:
    prefix = os.environ.get("MOMENTO_SET_PREFIX", MOMENTO_SET_PREFIX).strip() or MOMENTO_SET_PREFIX
    release = os.environ.get(MOMENTO_RELEASE_ENV, "").strip()
    if not release:
        return prefix
    return f"{prefix}:{release}"


def _secret_string_value(secret_name: str) -> str:
    secrets_client = _boto3_client("secretsmanager")
    response = secrets_client.get_secret_value(SecretId=secret_name)
    return str(response.get("SecretString", ""))


def _momento_token_from_secret(secret_name: str, secret_key: str) -> str:
    secret_string = _secret_string_value(secret_name)
    if not secret_string:
        return ""

    try:
        secret_payload = json.loads(secret_string)
    except json.JSONDecodeError:
        # Support plain-string secret values when the secret is not a JSON object.
        return secret_string

    value = secret_payload.get(secret_key)
    if value is None:
        return ""
    return str(value)


def _momento_endpoint_from_secret(secret_name: str, secret_key: str) -> str:
    secret_string = _secret_string_value(secret_name)
    if not secret_string:
        return ""

    try:
        secret_payload = json.loads(secret_string)
    except json.JSONDecodeError:
        return ""

    value = secret_payload.get(secret_key)
    if value is None:
        for fallback_key in ("MOMENTO_ENDPOINT", "endpoint", "momento_endpoint"):
            value = secret_payload.get(fallback_key)
            if value is not None:
                break

    if value is None:
        return ""
    return str(value)


def _momento_configuration(configurations):
    for provider_name in ("InRegion", "Laptop", "Lambda"):
        provider = getattr(configurations, provider_name, None)
        if provider is None:
            continue
        for selector_name in ("latest", "v1"):
            selector = getattr(provider, selector_name, None)
            if selector is None:
                continue
            try:
                return selector()
            except TypeError:
                continue
    return None


def _momento_normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        return ""

    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        value = parsed.netloc or parsed.path

    value = value.split("/", 1)[0]
    if value.endswith(":443"):
        value = value[:-4]

    for prefix in ("control.", "cache.", "token."):
        if value.startswith(prefix):
            return value[len(prefix):]

    return value


def _momento_credential_provider(credential_provider, auth_token: str, endpoint: str = ""):
    from_string = getattr(credential_provider, "from_string", None)
    fallback_error: BaseException | None = None

    if from_string is not None:
        try:
            return from_string(auth_token)
        except TypeError as exc:
            fallback_error = exc
        except Exception as exc:  # pragma: no cover - depends on Momento SDK internals
            message = str(exc).lower()
            if "v2 api key" not in message:
                raise
            fallback_error = exc

    normalized_endpoint = _momento_normalize_endpoint(endpoint)

    from_api_key_v2 = getattr(credential_provider, "from_api_key_v2", None)
    if from_api_key_v2 is not None:
        if not normalized_endpoint:
            raise RuntimeError(
                "Momento v2 API key requires MOMENTO_ENDPOINT (or MOMENTO_ENDPOINT value in secret)."
            )
        try:
            return from_api_key_v2(auth_token, normalized_endpoint)
        except TypeError as exc:
            fallback_error = exc

    for method_name in (
        "from_disposable_token",
        "from_api_key",
        "from_api_key_string",
    ):
        method = getattr(credential_provider, method_name, None)
        if method is None:
            continue
        try:
            return method(auth_token)
        except TypeError as exc:
            fallback_error = exc
            continue

    if fallback_error is not None:
        raise fallback_error

    raise RuntimeError("No supported Momento credential provider method found")


def _momento_cache_client(auth_token: str, endpoint: str = ""):
    from momento import CacheClient, Configurations, CredentialProvider  # type: ignore[import-not-found]

    configuration = _momento_configuration(Configurations)
    kwargs: dict[str, Any] = {
        "credential_provider": _momento_credential_provider(CredentialProvider, auth_token, endpoint),
    }

    if configuration is not None:
        kwargs["configuration"] = configuration

    try:
        return CacheClient(default_ttl=timedelta(days=30), **kwargs)
    except TypeError:
        return CacheClient(**kwargs)


def _momento_put_sorted_set_element(
    client,
    cache_name: str,
    set_name: str,
    score: str,
    value: str,
) -> None:
    score_float = float(score)
    call_attempts = (
        (
            (),
            {
                "cache_name": cache_name,
                "set_name": set_name,
                "value": value,
                "score": score_float,
            },
        ),
        ((cache_name, set_name, value, score_float), {}),
        ((cache_name, set_name, value, score), {}),
        (
            (),
            {
                "cache_name": cache_name,
                "set_name": set_name,
                "element": value,
                "score": score_float,
            },
        ),
    )

    last_error: BaseException | None = None
    for args, kwargs in call_attempts:
        try:
            client.sorted_set_put_element(*args, **kwargs)
            return
        except (TypeError, AttributeError, ValueError) as exc:  # pragma: no cover - depends on Momento SDK signature
            last_error = exc

    if last_error is not None:
        raise last_error


def _momento_request_min_interval_seconds() -> float:
    raw_value = os.environ.get(MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS_ENV, "").strip()
    if not raw_value:
        return MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS_DEFAULT

    try:
        min_interval = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid {MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS_ENV}: {raw_value}"
        ) from exc

    if min_interval < 0:
        raise RuntimeError(f"{MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS_ENV} must be non-negative")

    return min_interval


def _momento_wait_for_request_slot() -> None:
    min_interval = _momento_request_min_interval_seconds()
    if min_interval <= 0:
        return

    now = time.monotonic()
    last_request_at = float(_RUNTIME_STATE.get("last_momento_request_at", 0.0))
    elapsed = now - last_request_at if last_request_at else min_interval
    remaining = min_interval - elapsed
    if remaining > 0:
        time.sleep(remaining)

    _RUNTIME_STATE["last_momento_request_at"] = time.monotonic()


def _momento_put_sorted_set_elements(
    client,
    cache_name: str,
    set_name: str,
    elements: dict[str, float],
) -> None:
    try:
        response = client.sorted_set_put_elements(cache_name, set_name, elements)
    except Exception as exc:
        if _momento_is_rate_limit_error(exc):
            raise RuntimeError(
                f"Momento sorted_set_put_elements failed for cache {cache_name} set {set_name}: {exc}"
            ) from exc
        raise

    from momento.responses.data.sorted_set.put_elements import (  # type: ignore[import-not-found]
        CacheSortedSetPutElements,
    )

    if isinstance(response, CacheSortedSetPutElements.Error):
        raise RuntimeError(
            f"Momento sorted_set_put_elements failed for cache {cache_name} set {set_name}: {response.message}"
        )


def _momento_is_rate_limit_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    if "rate limit" in message or "rate-limit" in message:
        return True

    status_code = getattr(exc, "code", None)
    if callable(status_code):
        try:
            status_code = status_code()
        except Exception:
            status_code = None

    status_name = getattr(status_code, "name", "")
    if str(status_name).upper() == "RESOURCE_EXHAUSTED":
        return True

    return "resource_exhausted" in message or "resource exhausted" in message


def _momento_chunk_sorted_set_elements(elements: dict[str, float]) -> list[dict[str, float]]:
    chunks: list[dict[str, float]] = []
    current_chunk: dict[str, float] = {}

    for value, score in elements.items():
        candidate_chunk = dict(current_chunk)
        candidate_chunk[value] = score
        candidate_size = len(
            json.dumps(candidate_chunk, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )

        if current_chunk and candidate_size > MOMENTO_SORTED_SET_REQUEST_SIZE_BYTES:
            chunks.append(current_chunk)
            current_chunk = {}
            candidate_chunk = {value: score}
            candidate_size = len(
                json.dumps(candidate_chunk, separators=(",", ":"), sort_keys=True).encode("utf-8")
            )

        current_chunk[value] = score

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _momento_is_rate_limit_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "rate limit" in message or "rate-limit" in message


def _momento_put_sorted_set_elements_with_retry(
    client,
    cache_name: str,
    set_name: str,
    elements: dict[str, float],
) -> None:
    backoff_seconds = 1.0
    max_retries = 8

    for chunk in _momento_chunk_sorted_set_elements(elements):
        for attempt in range(max_retries + 1):
            try:
                _momento_wait_for_request_slot()
                _momento_put_sorted_set_elements(client, cache_name, set_name, chunk)
                break
            except RuntimeError as exc:
                if not _momento_is_rate_limit_error(exc) or attempt == max_retries:
                    raise

                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30.0)


def _momento_value_for_output_row(output_type: str, row: dict[str, str]) -> str:
    payload = {
        "ip_version": row["ip_version"],
        "prefix_len": row["prefix_len"],
        "range_start_hex": row["range_start_hex"],
        "range_end_hex": row["range_end_hex"],
        "network": row["network"],
    }

    if output_type == "asn":
        payload["asn"] = row["asn"]
        payload["organization"] = row["organization"]
    else:
        payload["country_iso_code"] = row["country_iso_code"]
        payload["country_name"] = row["country_name"]
        payload["subdivision"] = row["subdivision"]
        payload["city"] = row["city"]

    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _momento_sorted_set_batch_size() -> int:
    raw_value = os.environ.get(MOMENTO_SORTED_SET_BATCH_SIZE_ENV, "").strip()
    if not raw_value:
        return MOMENTO_SORTED_SET_BATCH_SIZE_DEFAULT

    try:
        batch_size = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {MOMENTO_SORTED_SET_BATCH_SIZE_ENV}: {raw_value}") from exc

    if batch_size <= 0:
        raise RuntimeError(f"{MOMENTO_SORTED_SET_BATCH_SIZE_ENV} must be greater than zero")

    return batch_size


def _momento_cache_name_for_source(source_key: str) -> str:
    cache_names_raw = os.environ.get(MOMENTO_CACHE_NAMES_BY_SOURCE_ENV, "").strip()
    if cache_names_raw:
        try:
            cache_names = json.loads(cache_names_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid {MOMENTO_CACHE_NAMES_BY_SOURCE_ENV} JSON"
            ) from exc

        cache_name = str(cache_names.get(source_key, "")).strip()
        if cache_name:
            return cache_name

        raise RuntimeError(f"No Momento cache configured for source {source_key}")

    cache_name = os.environ.get(MOMENTO_CACHE_NAME_ENV, "").strip()
    if cache_name:
        return cache_name

    raise RuntimeError("Missing Momento cache configuration")


def _momento_context(cache_name: str) -> dict[str, Any] | None:
    secret_name = os.environ.get("MOMENTO_SECRET_NAME", "").strip()
    secret_key = os.environ.get("MOMENTO_SECRET_KEY", MOMENTO_SECRET_KEY_DEFAULT).strip() or MOMENTO_SECRET_KEY_DEFAULT
    endpoint_secret_key = (
        os.environ.get("MOMENTO_ENDPOINT_SECRET_KEY", MOMENTO_ENDPOINT_SECRET_KEY_DEFAULT).strip()
        or MOMENTO_ENDPOINT_SECRET_KEY_DEFAULT
    )
    endpoint_override = os.environ.get("MOMENTO_ENDPOINT", "").strip()

    if not cache_name or not secret_name:
        return None

    token = _momento_token_from_secret(secret_name, secret_key)
    if not token:
        return None

    endpoint = endpoint_override or _momento_endpoint_from_secret(secret_name, endpoint_secret_key)

    client = _momento_cache_client(token, endpoint)
    return {
        "cache_name": cache_name,
        "client": client,
        "loaded_rows": 0,
        "set_names": set(),
    }


def _momento_add_output_row(
    momento_context: dict[str, Any] | None,
    output_key: str,
    output_type: str,
    row: dict[str, str],
) -> None:
    if momento_context is None:
        return
    if output_key not in MOMENTO_SOURCE_OUTPUTS:
        return

    set_name = row["momento_set"]
    _momento_put_sorted_set_element(
        momento_context["client"],
        momento_context["cache_name"],
        set_name,
        row["momento_score"],
        _momento_value_for_output_row(output_type, row),
    )
    momento_context["loaded_rows"] += 1
    momento_context["set_names"].add(set_name)


def _momento_add_output_rows(
    momento_context: dict[str, Any] | None,
    output_key: str,
    output_type: str,
    rows: list[dict[str, str]],
) -> None:
    del output_key
    if momento_context is None:
        return

    batch_by_set: dict[str, dict[str, float]] = {}
    for row in rows:
        set_name = row["momento_set"]
        elements = batch_by_set.setdefault(set_name, {})
        elements[_momento_value_for_output_row(output_type, row)] = float(row["momento_score"])

    for set_name, elements in batch_by_set.items():
        _momento_put_sorted_set_elements_with_retry(
            momento_context["client"],
            momento_context["cache_name"],
            set_name,
            elements,
        )
    momento_context["loaded_rows"] += len(rows)
    for set_name in batch_by_set:
        momento_context["set_names"].add(set_name)


def _momento_score_for_parsed_network(parsed_network: ipaddress._BaseNetwork) -> str:
    start_ip = int(parsed_network.network_address)

    # Momento sorted-set scores are numeric and precision-limited, so derive a
    # compact family-aware score from the network start address.
    if parsed_network.version == 4:
        family_bit = 0
        normalized_start = start_ip << 96
    else:
        family_bit = 1
        normalized_start = start_ip

    prefix = normalized_start >> (128 - 52)
    return str((family_bit << 52) | prefix)


def _momento_score_for_network(network: str) -> str:
    parsed_network = ipaddress.ip_network(network, strict=False)
    return _momento_score_for_parsed_network(parsed_network)


def _momento_shard_for_parsed_network(parsed_network: ipaddress._BaseNetwork) -> str:
    start_ip = int(parsed_network.network_address)
    if parsed_network.version == 4:
        normalized_start = start_ip << 96
    else:
        normalized_start = start_ip

    top16 = normalized_start >> (128 - 16)
    return f"v{parsed_network.version}:{top16:04x}"


def _momento_set_for_dataset_and_parsed_network(
    dataset: str,
    parsed_network: ipaddress._BaseNetwork,
) -> str:
    shard = _momento_shard_for_parsed_network(parsed_network)
    return f"{_momento_set_prefix()}:{dataset}:{shard}"


def _momento_sort_key_for_parsed_network(parsed_network: ipaddress._BaseNetwork) -> str:
    return f"{parsed_network.prefixlen:03d}"


def _normalized_bounds_for_parsed_network(
    parsed_network: ipaddress._BaseNetwork,
) -> tuple[int, int]:
    start_ip = int(parsed_network.network_address)
    end_ip = int(parsed_network.broadcast_address)
    if parsed_network.version == 4:
        return start_ip << 96, end_ip << 96
    return start_ip, end_ip


def _momento_range_hex_for_parsed_network(
    parsed_network: ipaddress._BaseNetwork,
) -> tuple[str, str]:
    normalized_start, normalized_end = _normalized_bounds_for_parsed_network(parsed_network)
    return f"{normalized_start:032x}", f"{normalized_end:032x}"


def momento_lookup_fields_for_ip(ip_text: str) -> dict[str, str]:
    parsed_ip = ipaddress.ip_address(ip_text)
    ip_int = int(parsed_ip)

    if parsed_ip.version == 4:
        family_bit = 0
        normalized_ip = ip_int << 96
    else:
        family_bit = 1
        normalized_ip = ip_int

    prefix = normalized_ip >> (128 - 52)
    score = str((family_bit << 52) | prefix)
    top16 = normalized_ip >> (128 - 16)
    shard = f"v{parsed_ip.version}:{top16:04x}"
    prefix_len = "32" if parsed_ip.version == 4 else "128"
    ip_hex = f"{normalized_ip:032x}"

    return {
        "momento_score": score,
        "ip_version": str(parsed_ip.version),
        "shard": shard,
        "sort_key": f"{int(prefix_len):03d}",
        "prefix_len": prefix_len,
        "ip_hex": ip_hex,
        "asn_momento_set": f"{_momento_set_prefix()}:asn:{shard}",
        "city_momento_set": f"{_momento_set_prefix()}:city:{shard}",
    }


def momento_lookup_score_for_ip(ip_text: str) -> str:
    return momento_lookup_fields_for_ip(ip_text)["momento_score"]


def _collect_city_geoname_ids(file_path: str) -> set[str]:
    geoname_ids: set[str] = set()

    with open(file_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return geoname_ids

        index = {name: position for position, name in enumerate(header)}
        geoname_index = index.get("geoname_id")
        registered_index = index.get("registered_country_geoname_id")

        for row in reader:
            geoname_value = ""
            if geoname_index is not None and geoname_index < len(row):
                geoname_value = row[geoname_index].strip()

            registered_value = ""
            if registered_index is not None and registered_index < len(row):
                registered_value = row[registered_index].strip()

            geoname_id = geoname_value or registered_value
            if geoname_id:
                geoname_ids.add(geoname_id)

    return geoname_ids


def _load_locations_subset(
    file_path: str,
    geoname_ids: set[str],
) -> dict[str, dict[str, str]]:
    if not geoname_ids:
        return {}

    locations = {}

    with open(file_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return locations

        index = {name: position for position, name in enumerate(header)}
        geoname_index = index.get("geoname_id")
        country_iso_code_index = index.get("country_iso_code")
        country_name_index = index.get("country_name")
        subdivision_index = index.get("subdivision_1_name")
        city_index = index.get("city_name")

        if geoname_index is None:
            return locations

        for row in reader:
            if geoname_index >= len(row):
                continue

            geoname_id = row[geoname_index].strip()
            if not geoname_id or geoname_id not in geoname_ids:
                continue

            subdivision = (
                row[subdivision_index].strip()
                if subdivision_index is not None and subdivision_index < len(row)
                else ""
            )
            city = (
                row[city_index].strip()
                if city_index is not None and city_index < len(row)
                else ""
            )

            if subdivision and city and subdivision == city:
                subdivision = ""

            locations[geoname_id] = {
                "country_iso_code": row[country_iso_code_index].strip()
                if country_iso_code_index is not None
                and country_iso_code_index < len(row)
                else "",
                "country_name": row[country_name_index].strip()
                if country_name_index is not None and country_name_index < len(row)
                else "",
                "subdivision": subdivision,
                "city": city,
            }

    return locations




def build_output_artifacts(directory: str) -> dict[str, dict[str, object]]:
    artifacts = {}

    for source_key in sorted(SOURCE_OUTPUT_CONFIG.keys()):
        config = SOURCE_OUTPUT_CONFIG[source_key]
        if config["type"] == "city":
            city_path = os.path.join(directory, source_key)
            locations_path = os.path.join(directory, "GeoLite2-City-Locations-en.csv")
            referenced_geoname_ids = _collect_city_geoname_ids(city_path)
            locations = _load_locations_subset(locations_path, referenced_geoname_ids)
            rows = list(_iter_city_output_rows(city_path, locations))
            output_fields = CITY_OUTPUT_FIELDS
        else:
            rows = list(_iter_asn_output_rows(os.path.join(directory, source_key)))
            output_fields = ASN_OUTPUT_FIELDS

        body = "\n".join(
            "|".join(row[field] for field in output_fields)
            for row in rows
        ) + ("\n" if rows else "")

        artifacts[config["output"]] = {
            "body": body,
            "summary": {
                "event": "processed_source_output_summary",
                "sourceKey": source_key,
                "output": config["output"],
                "outputRows": len(rows),
            },
        }

    return artifacts


def _build_outputs_from_directory(directory: str) -> dict[str, str]:
    return {
        output_key: artifact["body"]
        for output_key, artifact in build_output_artifacts(directory).items()
    }


def build_outputs_from_directory(directory: str) -> dict[str, str]:
    return _build_outputs_from_directory(directory)


def _download_named_sources(
    s3_client,
    bucket_name: str,
    file_names: list[str],
    directory: str,
) -> None:
    for file_name in file_names:
        s3_client.download_file(
            bucket_name,
            file_name,
            os.path.join(directory, file_name),
        )




def _source_metadata(
    s3_client,
    bucket_name: str,
    file_names: list[str],
) -> tuple[list[str], dict[str, str]]:
    missing = []
    signature_parts = {}

    for file_name in sorted(file_names):
        try:
            response = s3_client.head_object(Bucket=bucket_name, Key=file_name)
        except Exception as exc:
            response = getattr(exc, "response", {})
            error = response.get("Error", {})
            error_code = str(error.get("Code", ""))

            if error_code in {"404", "NoSuchKey", "NotFound"}:
                missing.append(file_name)
                continue

            raise

        etag = str(response.get("ETag", "")).strip('"')
        last_modified = response.get("LastModified")
        if hasattr(last_modified, "isoformat"):
            last_modified_text = last_modified.isoformat()
        else:
            last_modified_text = str(last_modified or "")

        signature_parts[file_name] = f"{etag}:{last_modified_text}"

    return missing, signature_parts


def _source_dependencies(source_key: str) -> list[str]:
    config = SOURCE_OUTPUT_CONFIG[source_key]
    if config["type"] == "city":
        return ["GeoLite2-City-Locations-en.csv", source_key]
    return [source_key]


def _source_signature(source_metadata: dict[str, str], source_key: str) -> str:
    parts = []
    for file_name in _source_dependencies(source_key):
        parts.append(f"{file_name}:{source_metadata.get(file_name, '')}")
    return "|".join(parts)


def _source_keys_from_relevant_records(relevant_records: list[dict[str, str]]) -> list[str]:
    source_keys = set()
    for record in relevant_records:
        key = record["key"].rsplit("/", 1)[-1]
        if key in SOURCE_OUTPUT_CONFIG:
            source_keys.add(key)
    return sorted(source_keys)


def _extract_relevant_records(event) -> list[dict[str, str]]:
    records = []

    for record in event.get("Records", []):
        if "body" in record:
            payload = json.loads(record.get("body", "{}"))
        else:
            payload = record

        if "Records" in payload:
            for s3_record in payload.get("Records", []):
                bucket_name = s3_record["s3"]["bucket"]["name"]
                object_key = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])
                records.append({"bucket": bucket_name, "key": object_key})
            continue

        detail = payload.get("detail", {})
        bucket = detail.get("bucket", {})
        obj = detail.get("object", {})
        bucket_name = bucket.get("name")
        object_key = obj.get("key")
        if bucket_name and object_key:
            records.append(
                {
                    "bucket": bucket_name,
                    "key": urllib.parse.unquote_plus(object_key),
                }
            )

    def trigger_key(key: str) -> str:
        return key.rsplit("/", 1)[-1]

    return [
        record for record in records if trigger_key(record["key"]) in TRIGGER_SOURCE_FILES
    ]


def _extract_source_jobs(event) -> list[dict[str, Any]]:
    jobs = []

    for record in event.get("Records", []):
        if "body" not in record:
            continue

        payload = json.loads(record.get("body", "{}"))
        if payload.get("jobType") != JOB_TYPE_SOURCE_BUILD:
            continue

        source_key = payload.get("sourceKey")
        if source_key not in SOURCE_OUTPUT_CONFIG:
            continue

        jobs.append(payload)

    return jobs


def _enqueue_source_jobs(
    sqs_client,
    queue_url: str,
    source_signatures: dict[str, str],
    source_keys: list[str],
) -> list[str]:
    queued_sources = []

    for source_key in source_keys:
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(
                {
                    "jobType": JOB_TYPE_SOURCE_BUILD,
                    "sourceKey": source_key,
                    "sourceSignature": source_signatures[source_key],
                },
                sort_keys=True,
            ),
        )
        queued_sources.append(source_key)

    return queued_sources


def _iter_asn_output_rows(asn_path: str):
    with open(asn_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return

        index = {name: position for position, name in enumerate(header)}
        network_index = index.get("network")
        asn_index = index.get("autonomous_system_number")
        organization_index = index.get("autonomous_system_organization")

        if network_index is None:
            return

        for row in reader:
            if network_index >= len(row):
                continue

            network = row[network_index].strip()
            if not network:
                continue

            asn_value = (
                row[asn_index].strip()
                if asn_index is not None and asn_index < len(row)
                else ""
            )
            organization_value = (
                row[organization_index].strip()
                if organization_index is not None and organization_index < len(row)
                else ""
            )

            parsed_network = ipaddress.ip_network(network, strict=False)
            range_start_hex, range_end_hex = _momento_range_hex_for_parsed_network(parsed_network)
            yield {
                "momento_score": _momento_score_for_parsed_network(parsed_network),
                "ip_version": str(parsed_network.version),
                "shard": _momento_shard_for_parsed_network(parsed_network),
                "momento_set": _momento_set_for_dataset_and_parsed_network("asn", parsed_network),
                "sort_key": _momento_sort_key_for_parsed_network(parsed_network),
                "prefix_len": str(parsed_network.prefixlen),
                "range_start_hex": range_start_hex,
                "range_end_hex": range_end_hex,
                "network": network,
                "asn": asn_value,
                "organization": organization_value,
            }


def _iter_city_output_rows(city_path: str, locations: dict[str, dict[str, str]]):
    with open(city_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return

        index = {name: position for position, name in enumerate(header)}
        network_index = index.get("network")
        geoname_index = index.get("geoname_id")
        registered_index = index.get("registered_country_geoname_id")

        if network_index is None:
            return

        for row in reader:
            if network_index >= len(row):
                continue

            network = row[network_index].strip()
            if not network:
                continue

            geoname_value = ""
            if geoname_index is not None and geoname_index < len(row):
                geoname_value = row[geoname_index].strip()

            registered_value = ""
            if registered_index is not None and registered_index < len(row):
                registered_value = row[registered_index].strip()

            geoname_id = geoname_value or registered_value
            location = locations.get(geoname_id, {})
            parsed_network = ipaddress.ip_network(network, strict=False)
            range_start_hex, range_end_hex = _momento_range_hex_for_parsed_network(parsed_network)

            yield {
                "momento_score": _momento_score_for_parsed_network(parsed_network),
                "ip_version": str(parsed_network.version),
                "shard": _momento_shard_for_parsed_network(parsed_network),
                "momento_set": _momento_set_for_dataset_and_parsed_network("city", parsed_network),
                "sort_key": _momento_sort_key_for_parsed_network(parsed_network),
                "prefix_len": str(parsed_network.prefixlen),
                "range_start_hex": range_start_hex,
                "range_end_hex": range_end_hex,
                "network": network,
                "country_iso_code": str(location.get("country_iso_code", "")),
                "country_name": str(location.get("country_name", "")),
                "subdivision": str(location.get("subdivision", "")),
                "city": str(location.get("city", "")),
            }


def _process_source_job(
    s3_client,
    download_bucket_name: str,
    processed_bucket_name: str,
    source_key: str,
    momento_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        config = SOURCE_OUTPUT_CONFIG[source_key]
        dependencies = _source_dependencies(source_key)
        _download_named_sources(s3_client, download_bucket_name, dependencies, directory)
        output_key = config["output"]

        output_rows = 0
        momento_rows: list[dict[str, str]] = []

        mpu = s3_client.create_multipart_upload(
            Bucket=processed_bucket_name,
            Key=output_key,
            ContentType="text/plain",
        )
        upload_id = mpu["UploadId"]
        parts = []
        part_number = 1
        buffer = io.BytesIO()
        try:
            if config["type"] == "city":
                city_path = os.path.join(directory, source_key)
                locations_path = os.path.join(directory, "GeoLite2-City-Locations-en.csv")
                referenced_geoname_ids = _collect_city_geoname_ids(city_path)
                locations = _load_locations_subset(locations_path, referenced_geoname_ids)
                rows = _iter_city_output_rows(city_path, locations)
                output_fields = CITY_OUTPUT_FIELDS
            else:
                rows = _iter_asn_output_rows(os.path.join(directory, source_key))
                output_fields = ASN_OUTPUT_FIELDS

            for row in rows:
                line = "|".join(row[field] for field in output_fields) + "\n"
                buffer.write(line.encode("utf-8"))
                output_rows += 1
                if momento_context is not None and output_key in MOMENTO_SOURCE_OUTPUTS:
                    momento_rows.append(row)

                if momento_context is not None and len(momento_rows) >= _momento_sorted_set_batch_size():
                    _momento_add_output_rows(
                        momento_context,
                        output_key,
                        config["type"],
                        momento_rows,
                    )
                    momento_rows = []

            if momento_context is not None and momento_rows:
                _momento_add_output_rows(
                    momento_context,
                    output_key,
                    config["type"],
                    momento_rows,
                )

            if buffer.tell() >= _S3_MULTIPART_CHUNK_SIZE:
                buffer.seek(0)
                response = s3_client.upload_part(
                    Bucket=processed_bucket_name,
                    Key=output_key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=buffer,
                )
                parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                part_number += 1
                buffer = io.BytesIO()

            remaining = buffer.tell()
            if remaining > 0:
                buffer.seek(0)
                response = s3_client.upload_part(
                    Bucket=processed_bucket_name,
                    Key=output_key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=buffer,
                )
                parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
            elif not parts:
                # Empty output — upload an empty part to satisfy multipart requirements
                buffer.seek(0)
                response = s3_client.upload_part(
                    Bucket=processed_bucket_name,
                    Key=output_key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=buffer,
                )
                parts.append({"PartNumber": part_number, "ETag": response["ETag"]})

            s3_client.complete_multipart_upload(
                Bucket=processed_bucket_name,
                Key=output_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            s3_client.abort_multipart_upload(
                Bucket=processed_bucket_name,
                Key=output_key,
                UploadId=upload_id,
            )
            raise

        _log_event(
            "processed_source_output_summary",
            sourceKey=source_key,
            output=output_key,
            outputRows=output_rows,
        )

    return {
        "sourceKey": source_key,
        "output": output_key,
        "outputRows": output_rows,
    }


def handler(event, context):
    started_at = time.perf_counter()
    request_id = getattr(context, "aws_request_id", "")

    download_bucket_name = os.environ["DOWNLOAD_BUCKET_NAME"]
    processed_bucket_name = os.environ["PROCESSED_BUCKET_NAME"]
    relevant_records = _extract_relevant_records(event)
    source_jobs = _extract_source_jobs(event)

    _log_event(
        "processed_run_start",
        requestId=request_id,
        downloadBucket=download_bucket_name,
        processedBucket=processed_bucket_name,
        sourceJobCount=len(source_jobs),
        relevantRecordCount=len(relevant_records),
        relevantKeys=sorted({record["key"] for record in relevant_records}),
    )

    s3_client = _boto3_client("s3")

    if source_jobs:
        processed_jobs = []
        for source_job in source_jobs:
            source_key = str(source_job["sourceKey"])
            momento_context = _momento_context(_momento_cache_name_for_source(source_key))
            processed_jobs.append(
                _process_source_job(
                    s3_client,
                    download_bucket_name,
                    processed_bucket_name,
                    source_key,
                    momento_context,
                )
            )

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        outputs = sorted(job["output"] for job in processed_jobs)
        _log_event(
            "processed_run_result",
            requestId=request_id,
            skipped=False,
            skipReason="",
            outputs=outputs,
            missingSourceFiles=[],
            durationMs=duration_ms,
            mode="worker",
        )
        if momento_context is None:
            _log_event(
                "momento_load_skipped",
                requestId=request_id,
                reason="missing_momento_environment",
                outputs=outputs,
            )
        else:
            _log_event(
                "momento_load_summary",
                requestId=request_id,
                cacheName=momento_context["cache_name"],
                setCount=len(momento_context["set_names"]),
                loadedRows=momento_context["loaded_rows"],
                outputs=outputs,
            )

        return {
            "statusCode": 200,
            "downloadBucket": download_bucket_name,
            "processedBucket": processed_bucket_name,
            "processed": [],
            "outputs": outputs,
            "skipped": False,
            "skipReason": "",
            "missingSourceFiles": [],
            "mode": "worker",
            "momentoLoadedRows": 0 if momento_context is None else momento_context["loaded_rows"],
            "momentoSetCount": 0 if momento_context is None else len(momento_context["set_names"]),
            "momentoEnabled": momento_context is not None,
        }

    if not relevant_records:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _log_event(
            "processed_run_result",
            requestId=request_id,
            skipped=True,
            skipReason="no_relevant_records",
            outputs=[],
            missingSourceFiles=[],
            durationMs=duration_ms,
            mode="coordinator",
        )
        return {
            "statusCode": 200,
            "downloadBucket": download_bucket_name,
            "processedBucket": processed_bucket_name,
            "processed": [],
            "outputs": [],
            "skipped": True,
            "skipReason": "no_relevant_records",
            "missingSourceFiles": [],
            "mode": "coordinator",
        }

    for record in relevant_records:
        print(f"Received source update for s3://{record['bucket']}/{record['key']}")

    impacted_source_keys = _source_keys_from_relevant_records(relevant_records)
    required_files = set()
    for source_key in impacted_source_keys:
        required_files.update(_source_dependencies(source_key))

    missing_files, source_metadata = _source_metadata(
        s3_client,
        download_bucket_name,
        sorted(required_files),
    )

    if missing_files:
        print(
            "Skipping rebuild until required source files exist: "
            + ", ".join(missing_files)
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _log_event(
            "processed_run_result",
            requestId=request_id,
            skipped=True,
            skipReason="missing_source_files",
            outputs=[],
            missingSourceFiles=missing_files,
            durationMs=duration_ms,
            mode="coordinator",
        )
        return {
            "statusCode": 200,
            "downloadBucket": download_bucket_name,
            "processedBucket": processed_bucket_name,
            "processed": relevant_records,
            "outputs": [],
            "skipped": True,
            "skipReason": "missing_source_files",
            "missingSourceFiles": missing_files,
            "mode": "coordinator",
        }

    source_signatures = {
        source_key: _source_signature(source_metadata, source_key)
        for source_key in impacted_source_keys
    }
    sources_to_queue = [
        source_key
        for source_key in impacted_source_keys
        if source_signatures[source_key]
        != _RUNTIME_STATE["last_processed_source_signatures"].get(source_key, "")
    ]

    if not sources_to_queue:
        print("Skipping rebuild because impacted source files are unchanged")
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        _log_event(
            "processed_run_result",
            requestId=request_id,
            skipped=True,
            skipReason="source_unchanged",
            outputs=[],
            missingSourceFiles=[],
            durationMs=duration_ms,
            mode="coordinator",
            impactedSourceKeys=impacted_source_keys,
        )
        return {
            "statusCode": 200,
            "downloadBucket": download_bucket_name,
            "processedBucket": processed_bucket_name,
            "processed": relevant_records,
            "outputs": [],
            "skipped": True,
            "skipReason": "source_unchanged",
            "missingSourceFiles": [],
            "mode": "coordinator",
            "impactedSourceKeys": impacted_source_keys,
        }

    queue_url = os.environ["PROCESS_QUEUE_URL"]
    sqs_client = _boto3_client("sqs")
    queued_source_keys = _enqueue_source_jobs(
        sqs_client,
        queue_url,
        source_signatures,
        sources_to_queue,
    )

    for source_key in queued_source_keys:
        _RUNTIME_STATE["last_processed_source_signatures"][source_key] = (
            source_signatures[source_key]
        )

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    _log_event(
        "processed_run_result",
        requestId=request_id,
        skipped=True,
        skipReason="queued_source_jobs",
        outputs=[],
        missingSourceFiles=[],
        durationMs=duration_ms,
        queuedSourceKeys=queued_source_keys,
        mode="coordinator",
        impactedSourceKeys=impacted_source_keys,
    )

    return {
        "statusCode": 200,
        "downloadBucket": download_bucket_name,
        "processedBucket": processed_bucket_name,
        "processed": relevant_records,
        "outputs": [],
        "skipped": True,
        "skipReason": "queued_source_jobs",
        "missingSourceFiles": [],
        "queuedSourceKeys": queued_source_keys,
        "mode": "coordinator",
        "impactedSourceKeys": impacted_source_keys,
    }

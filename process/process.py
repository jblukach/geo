import csv
import io
import ipaddress
import json
import os
import asyncio
import datetime
import tempfile
import time
import urllib.parse
from typing import Any

_S3_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB (S3 minimum part size is 5 MB)


TRIGGER_SOURCE_FILES = {
    "GeoLite2-ASN-Blocks-IPv4.csv",
    "GeoLite2-ASN-Blocks-IPv6.csv",
    "GeoLite2-City-Blocks-IPv4.csv",
    "GeoLite2-City-Blocks-IPv6.csv",
}

JOB_TYPE_SOURCE_BUILD = "source_build"
VALKEY_SORTED_SET_BATCH_SIZE_ENV = "VALKEY_SORTED_SET_BATCH_SIZE"
VALKEY_SORTED_SET_BATCH_SIZE_DEFAULT = 5000
VALKEY_ASN_V4_SET_NAME_ENV = "VALKEY_ASN_V4_SET_NAME"
VALKEY_ASN_V6_SET_NAME_ENV = "VALKEY_ASN_V6_SET_NAME"
VALKEY_CITY_V4_SET_NAME_ENV = "VALKEY_CITY_V4_SET_NAME"
VALKEY_CITY_V6_SET_NAME_ENV = "VALKEY_CITY_V6_SET_NAME"
VALKEY_ASN_V4_SET_NAME_DEFAULT = "asn_v4_ranges"
VALKEY_ASN_V6_SET_NAME_DEFAULT = "asn_v6_ranges"
VALKEY_CITY_V4_SET_NAME_DEFAULT = "city_v4_ranges"
VALKEY_CITY_V6_SET_NAME_DEFAULT = "city_v6_ranges"
VALKEY_LAST_UPDATED_ASN_KEY_ENV = "VALKEY_LAST_UPDATED_ASN_KEY"
VALKEY_LAST_UPDATED_CITY_KEY_ENV = "VALKEY_LAST_UPDATED_CITY_KEY"
VALKEY_LAST_UPDATED_ASN_KEY_DEFAULT = "geo:last_updated:asn"
VALKEY_LAST_UPDATED_CITY_KEY_DEFAULT = "geo:last_updated:city"
VALKEY_PORT_ENV = "VALKEY_PORT"
VALKEY_PORT_DEFAULT = 6379
VALKEY_TLS_ENV = "VALKEY_TLS"
VALKEY_MAX_CONNECTIONS_ENV = "VALKEY_MAX_CONNECTIONS"
VALKEY_MAX_CONNECTIONS_DEFAULT = 8

ASN_OUTPUT_FIELDS = (
    "range_start_int",
    "ip_version",
    "shard",
    "set_name",
    "sort_key",
    "prefix_len",
    "range_start_hex",
    "range_end_hex",
    "network",
    "asn",
    "organization",
)

CITY_OUTPUT_FIELDS = (
    "range_start_int",
    "ip_version",
    "shard",
    "set_name",
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

VALKEY_SOURCE_OUTPUTS = {
    config["output"] for config in SOURCE_OUTPUT_CONFIG.values()
}


_RUNTIME_STATE = {
    "last_processed_source_signatures": {
        key: "" for key in SOURCE_OUTPUT_CONFIG
    },
}


def reset_runtime_state() -> None:
    _RUNTIME_STATE["last_processed_source_signatures"] = {
        key: "" for key in SOURCE_OUTPUT_CONFIG
    }


def _boto3_client(service_name):
    import boto3  # type: ignore[import-not-found]

    return boto3.client(service_name)


def _log_event(event_name: str, **fields) -> None:
    payload = {"event": event_name}
    payload.update(fields)
    print(json.dumps(payload, sort_keys=True))


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        return ""

    if "://" in value:
        value = value.split("://", 1)[1]

    value = value.split("/", 1)[0]
    return value


def _valkey_endpoint() -> str:
    endpoint = os.environ.get("VALKEY_ENDPOINT", "").strip()
    return _normalize_endpoint(endpoint)


def _valkey_port() -> int:
    raw_value = os.environ.get(VALKEY_PORT_ENV, str(VALKEY_PORT_DEFAULT)).strip() or str(VALKEY_PORT_DEFAULT)
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {VALKEY_PORT_ENV}: {raw_value}") from exc
    if port <= 0:
        raise RuntimeError(f"{VALKEY_PORT_ENV} must be greater than zero")
    return port


def _valkey_max_connections() -> int:
    raw_value = os.environ.get(VALKEY_MAX_CONNECTIONS_ENV, str(VALKEY_MAX_CONNECTIONS_DEFAULT)).strip()
    if not raw_value:
        return VALKEY_MAX_CONNECTIONS_DEFAULT
    try:
        max_connections = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {VALKEY_MAX_CONNECTIONS_ENV}: {raw_value}") from exc
    if max_connections <= 0:
        raise RuntimeError(f"{VALKEY_MAX_CONNECTIONS_ENV} must be greater than zero")
    return max_connections


def _valkey_tls_enabled() -> bool:
    raw_value = os.environ.get(VALKEY_TLS_ENV, "true")
    return _is_truthy(raw_value)


def _valkey_context() -> dict[str, Any] | None:
    endpoint = _valkey_endpoint()
    if not endpoint:
        return None

    import redis.asyncio as redis  # type: ignore[import-not-found]

    pool_kwargs: dict[str, Any] = {
        "host": endpoint,
        "port": _valkey_port(),
        "decode_responses": True,
        "max_connections": _valkey_max_connections(),
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
    }
    if _valkey_tls_enabled():
        pool_kwargs["connection_class"] = redis.SSLConnection

    pool = redis.ConnectionPool(
        **pool_kwargs,
    )
    client = redis.Redis(connection_pool=pool)
    valkey_loop = asyncio.new_event_loop()
    return {
        "endpoint": endpoint,
        "pool": pool,
        "client": client,
        "loop": valkey_loop,
        "loaded_rows": 0,
        "set_names": set(),
    }


def _run_valkey_coroutine(valkey_context: dict[str, Any], coroutine):
    loop = valkey_context.get("loop")
    if loop is None:
        return asyncio.run(coroutine)
    if loop.is_closed():
        raise RuntimeError("Valkey event loop is closed")
    return loop.run_until_complete(coroutine)


def _close_valkey_context(valkey_context: dict[str, Any] | None) -> None:
    if valkey_context is None:
        return

    loop = valkey_context.get("loop")
    if loop is None or loop.is_closed():
        return

    try:
        loop.run_until_complete(valkey_context["client"].aclose())
        loop.run_until_complete(valkey_context["pool"].aclose())
    finally:
        loop.close()


def _valkey_sorted_set_batch_size() -> int:
    raw_value = os.environ.get(VALKEY_SORTED_SET_BATCH_SIZE_ENV, "").strip()
    if not raw_value:
        return VALKEY_SORTED_SET_BATCH_SIZE_DEFAULT

    try:
        batch_size = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {VALKEY_SORTED_SET_BATCH_SIZE_ENV}: {raw_value}") from exc

    if batch_size <= 0:
        raise RuntimeError(f"{VALKEY_SORTED_SET_BATCH_SIZE_ENV} must be greater than zero")

    return batch_size


def _valkey_set_name_for_dataset_and_ip_version(dataset: str, ip_version_text: str) -> str:
    ip_version = str(ip_version_text)
    if dataset == "asn" and ip_version == "4":
        return (
            os.environ.get(VALKEY_ASN_V4_SET_NAME_ENV, VALKEY_ASN_V4_SET_NAME_DEFAULT).strip()
            or VALKEY_ASN_V4_SET_NAME_DEFAULT
        )
    if dataset == "asn" and ip_version == "6":
        return (
            os.environ.get(VALKEY_ASN_V6_SET_NAME_ENV, VALKEY_ASN_V6_SET_NAME_DEFAULT).strip()
            or VALKEY_ASN_V6_SET_NAME_DEFAULT
        )
    if dataset == "city" and ip_version == "4":
        return (
            os.environ.get(VALKEY_CITY_V4_SET_NAME_ENV, VALKEY_CITY_V4_SET_NAME_DEFAULT).strip()
            or VALKEY_CITY_V4_SET_NAME_DEFAULT
        )
    if dataset == "city" and ip_version == "6":
        return (
            os.environ.get(VALKEY_CITY_V6_SET_NAME_ENV, VALKEY_CITY_V6_SET_NAME_DEFAULT).strip()
            or VALKEY_CITY_V6_SET_NAME_DEFAULT
        )
    raise RuntimeError(f"Unsupported dataset/ip_version combination: {dataset}/{ip_version}")


def _valkey_last_updated_key(dataset: str) -> str:
    if dataset == "asn":
        return (
            os.environ.get(VALKEY_LAST_UPDATED_ASN_KEY_ENV, VALKEY_LAST_UPDATED_ASN_KEY_DEFAULT).strip()
            or VALKEY_LAST_UPDATED_ASN_KEY_DEFAULT
        )
    if dataset == "city":
        return (
            os.environ.get(VALKEY_LAST_UPDATED_CITY_KEY_ENV, VALKEY_LAST_UPDATED_CITY_KEY_DEFAULT).strip()
            or VALKEY_LAST_UPDATED_CITY_KEY_DEFAULT
        )
    raise RuntimeError(f"Unsupported dataset for last-updated key: {dataset}")


def _utc_now_iso8601() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _valkey_escape_member_field(value: str) -> str:
    return value.replace("|", " ").strip()


def _valkey_member_for_output_row(output_type: str, row: dict[str, str]) -> str:
    fields = [str(row["range_end_int"]), output_type, row["network"], row["ip_version"]]
    if output_type == "asn":
        fields.extend([row["asn"], row["organization"]])
    else:
        fields.extend([
            row["country_iso_code"],
            row["country_name"],
            row["subdivision"],
            row["city"],
        ])
    return "|".join(_valkey_escape_member_field(str(field)) for field in fields)


async def _valkey_add_output_rows(
    valkey_context: dict[str, Any] | None,
    output_type: str,
    rows: list[dict[str, str]],
) -> None:
    if valkey_context is None or not rows:
        return

    batch_by_set: dict[str, dict[str, int]] = {}
    for row in rows:
        set_name = _valkey_set_name_for_dataset_and_ip_version(output_type, row["ip_version"])
        members = batch_by_set.setdefault(set_name, {})
        members[_valkey_member_for_output_row(output_type, row)] = int(row["range_start_int"])

    pipeline = valkey_context["client"].pipeline(transaction=False)
    for set_name, members in batch_by_set.items():
        pipeline.zadd(set_name, members)
        valkey_context["set_names"].add(set_name)

    await pipeline.execute()
    valkey_context["loaded_rows"] += len(rows)


async def _valkey_set_last_updated(
    valkey_context: dict[str, Any] | None,
    dataset: str,
) -> None:
    if valkey_context is None:
        return

    timestamp = _utc_now_iso8601()
    key = _valkey_last_updated_key(dataset)
    await valkey_context["client"].set(key, timestamp)


async def valkey_lookup_member(client, ip_text: str, dataset: str) -> str | None:
    parsed_ip = ipaddress.ip_address(ip_text)
    ip_int = int(parsed_ip)
    set_name = _valkey_set_name_for_dataset_and_ip_version(dataset, str(parsed_ip.version))

    matches = await client.zrangebyscore(
        set_name,
        0,
        ip_int,
        start=0,
        num=1,
        desc=True,
        withscores=False,
    )
    if not matches:
        return None

    member = str(matches[0])
    parts = member.split("|")
    if not parts:
        return None

    try:
        end_ip_int = int(parts[0])
    except ValueError:
        return None

    if ip_int > end_ip_int:
        return None
    return member


def _normalized_bounds_for_parsed_network(
    parsed_network: ipaddress._BaseNetwork,
) -> tuple[int, int]:
    return int(parsed_network.network_address), int(parsed_network.broadcast_address)


def _range_hex_for_parsed_network(
    parsed_network: ipaddress._BaseNetwork,
) -> tuple[str, str]:
    normalized_start, normalized_end = _normalized_bounds_for_parsed_network(parsed_network)
    return f"{normalized_start:032x}", f"{normalized_end:032x}"


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


def _cleanup_downloaded_sources(directory: str, file_names: list[str]) -> None:
    for file_name in file_names:
        file_path = os.path.join(directory, file_name)
        try:
            os.remove(file_path)
        except FileNotFoundError:
            continue




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
            range_start_int, range_end_int = _normalized_bounds_for_parsed_network(parsed_network)
            range_start_hex, range_end_hex = _range_hex_for_parsed_network(parsed_network)
            set_name = _valkey_set_name_for_dataset_and_ip_version("asn", str(parsed_network.version))
            yield {
                "range_start_int": str(range_start_int),
                "ip_version": str(parsed_network.version),
                "shard": f"v{parsed_network.version}",
                "set_name": set_name,
                "sort_key": f"{parsed_network.prefixlen:03d}",
                "prefix_len": str(parsed_network.prefixlen),
                "range_start_hex": range_start_hex,
                "range_end_hex": range_end_hex,
                "range_end_int": str(range_end_int),
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
            range_start_int, range_end_int = _normalized_bounds_for_parsed_network(parsed_network)
            range_start_hex, range_end_hex = _range_hex_for_parsed_network(parsed_network)
            set_name = _valkey_set_name_for_dataset_and_ip_version("city", str(parsed_network.version))

            yield {
                "range_start_int": str(range_start_int),
                "ip_version": str(parsed_network.version),
                "shard": f"v{parsed_network.version}",
                "set_name": set_name,
                "sort_key": f"{parsed_network.prefixlen:03d}",
                "prefix_len": str(parsed_network.prefixlen),
                "range_start_hex": range_start_hex,
                "range_end_hex": range_end_hex,
                "range_end_int": str(range_end_int),
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
    valkey_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        config = SOURCE_OUTPUT_CONFIG[source_key]
        dependencies = _source_dependencies(source_key)
        _download_named_sources(s3_client, download_bucket_name, dependencies, directory)
        output_key = config["output"]

        output_rows = 0
        valkey_rows: list[dict[str, str]] = []

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
                if valkey_context is not None and output_key in VALKEY_SOURCE_OUTPUTS:
                    valkey_rows.append(row)

                if valkey_context is not None and len(valkey_rows) >= _valkey_sorted_set_batch_size():
                    _run_valkey_coroutine(
                        valkey_context,
                        _valkey_add_output_rows(
                            valkey_context,
                            config["type"],
                            valkey_rows,
                        )
                    )
                    valkey_rows = []

            if valkey_context is not None and valkey_rows:
                _run_valkey_coroutine(
                    valkey_context,
                    _valkey_add_output_rows(
                        valkey_context,
                        config["type"],
                        valkey_rows,
                    )
                )

            if valkey_context is not None and output_rows > 0:
                _run_valkey_coroutine(
                    valkey_context,
                    _valkey_set_last_updated(
                        valkey_context,
                        config["type"],
                    )
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
        finally:
            _cleanup_downloaded_sources(directory, dependencies)

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
        valkey_context = _valkey_context()
        processed_jobs = []
        try:
            for source_job in source_jobs:
                source_key = str(source_job["sourceKey"])
                processed_jobs.append(
                    _process_source_job(
                        s3_client,
                        download_bucket_name,
                        processed_bucket_name,
                        source_key,
                        valkey_context,
                    )
                )
        finally:
            _close_valkey_context(valkey_context)

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
        if valkey_context is None:
            _log_event(
                "valkey_load_skipped",
                requestId=request_id,
                reason="missing_valkey_environment",
                outputs=outputs,
            )
        else:
            _log_event(
                "valkey_load_summary",
                requestId=request_id,
                endpoint=valkey_context["endpoint"],
                setCount=len(valkey_context["set_names"]),
                loadedRows=valkey_context["loaded_rows"],
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
            "valkeyLoadedRows": 0 if valkey_context is None else valkey_context["loaded_rows"],
            "valkeySetCount": 0 if valkey_context is None else len(valkey_context["set_names"]),
            "valkeyEnabled": valkey_context is not None,
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

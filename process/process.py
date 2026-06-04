import csv
import ipaddress
import json
import os
import tempfile
import time
import urllib.parse
from typing import Any


SOURCE_FILES = {
    "GeoLite2-ASN-Blocks-IPv4.csv",
    "GeoLite2-ASN-Blocks-IPv6.csv",
    "GeoLite2-City-Blocks-IPv4.csv",
    "GeoLite2-City-Blocks-IPv6.csv",
    "GeoLite2-City-Locations-en.csv",
}

TRIGGER_SOURCE_FILES = {
    "GeoLite2-ASN-Blocks-IPv4.csv",
    "GeoLite2-ASN-Blocks-IPv6.csv",
    "GeoLite2-City-Blocks-IPv4.csv",
    "GeoLite2-City-Blocks-IPv6.csv",
}

JOB_TYPE_FAMILY_BUILD = "family_build"

OUTPUT_FIELDS = (
    "startip",
    "endip",
    "network",
    "asn",
    "organization",
    "continent_code",
    "country_iso_code",
    "subdivision",
    "city",
)

IPV6_SCORE_BITS = 53
IPV6_SCORE_MASK = (1 << IPV6_SCORE_BITS) - 1

FAMILY_CONFIG = {
    4: {
        "city": "GeoLite2-City-Blocks-IPv4.csv",
        "asn": "GeoLite2-ASN-Blocks-IPv4.csv",
        "output": "GeoLite2-IPv4.txt",
    },
    6: {
        "city": "GeoLite2-City-Blocks-IPv6.csv",
        "asn": "GeoLite2-ASN-Blocks-IPv6.csv",
        "output": "GeoLite2-IPv6.txt",
    },
}


_RUNTIME_STATE = {
    "last_processed_family_signatures": {
        4: "",
        6: "",
    },
}


def reset_runtime_state() -> None:
    _RUNTIME_STATE["last_processed_family_signatures"] = {4: "", 6: ""}


def _boto3_client(service_name):
    import boto3  # type: ignore[import-not-found]

    return boto3.client(service_name)


def _log_event(event_name: str, **fields) -> None:
    payload = {"event": event_name}
    payload.update(fields)
    print(json.dumps(payload, sort_keys=True))


def _blank_output_row() -> dict[str, str]:
    return {field: "" for field in OUTPUT_FIELDS}


def _interval_from_network(network: str) -> tuple[int, int]:
    parsed_network = ipaddress.ip_network(network, strict=False)
    return int(parsed_network.network_address), int(parsed_network.broadcast_address)


def _load_locations(file_path: str) -> dict[str, dict[str, str]]:
    locations = {}

    with open(file_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return locations

        index = {name: position for position, name in enumerate(header)}
        geoname_index = index.get("geoname_id")
        continent_code_index = index.get("continent_code")
        country_iso_code_index = index.get("country_iso_code")
        subdivision_index = index.get("subdivision_1_name")
        city_index = index.get("city_name")

        if geoname_index is None:
            return locations

        for row in reader:
            if geoname_index >= len(row):
                continue

            geoname_id = row[geoname_index].strip()
            if not geoname_id:
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
                "continent_code": row[continent_code_index].strip()
                if continent_code_index is not None and continent_code_index < len(row)
                else "",
                "country_iso_code": row[country_iso_code_index].strip()
                if country_iso_code_index is not None
                and country_iso_code_index < len(row)
                else "",
                "subdivision": subdivision,
                "city": city,
            }

    return locations


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
        continent_code_index = index.get("continent_code")
        country_iso_code_index = index.get("country_iso_code")
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
                "continent_code": row[continent_code_index].strip()
                if continent_code_index is not None and continent_code_index < len(row)
                else "",
                "country_iso_code": row[country_iso_code_index].strip()
                if country_iso_code_index is not None
                and country_iso_code_index < len(row)
                else "",
                "subdivision": subdivision,
                "city": city,
            }

    return locations


def _read_city_intervals(
    file_path: str,
    locations: dict[str, dict[str, str]],
) -> list[dict[str, object]]:
    intervals = []
    requires_sort = False
    previous_start = -1

    with open(file_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return intervals

        index = {name: position for position, name in enumerate(header)}
        network_index = index.get("network")
        geoname_index = index.get("geoname_id")
        registered_index = index.get("registered_country_geoname_id")

        if network_index is None:
            return intervals

        for row in reader:
            if network_index >= len(row):
                continue

            network = row[network_index].strip()
            if not network:
                continue

            start_ip, end_ip = _interval_from_network(network)
            if start_ip < previous_start:
                requires_sort = True
            previous_start = start_ip

            geoname_value = ""
            if geoname_index is not None and geoname_index < len(row):
                geoname_value = row[geoname_index].strip()

            registered_value = ""
            if registered_index is not None and registered_index < len(row):
                registered_value = row[registered_index].strip()

            geoname_id = geoname_value or registered_value
            location = locations.get(geoname_id, {})

            intervals.append(
                {
                    "start": start_ip,
                    "end": end_ip,
                    "continent_code": location.get("continent_code", ""),
                    "country_iso_code": location.get("country_iso_code", ""),
                    "subdivision": location.get("subdivision", ""),
                    "city": location.get("city", ""),
                }
            )

    if requires_sort:
        intervals.sort(key=lambda interval: (interval["start"], interval["end"]))
    return intervals


def _read_asn_intervals(file_path: str) -> list[dict[str, object]]:
    intervals = []
    requires_sort = False
    previous_start = -1

    with open(file_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return intervals

        index = {name: position for position, name in enumerate(header)}
        network_index = index.get("network")
        asn_index = index.get("autonomous_system_number")
        organization_index = index.get("autonomous_system_organization")

        if network_index is None:
            return intervals

        for row in reader:
            if network_index >= len(row):
                continue

            network = row[network_index].strip()
            if not network:
                continue

            start_ip, end_ip = _interval_from_network(network)
            if start_ip < previous_start:
                requires_sort = True
            previous_start = start_ip

            asn_value = ""
            if asn_index is not None and asn_index < len(row):
                asn_value = row[asn_index].strip()

            organization_value = ""
            if organization_index is not None and organization_index < len(row):
                organization_value = row[organization_index].strip()

            intervals.append(
                {
                    "start": start_ip,
                    "end": end_ip,
                    "asn": asn_value,
                    "organization": organization_value,
                }
            )

    if requires_sort:
        intervals.sort(key=lambda interval: (interval["start"], interval["end"]))
    return intervals


def _iter_city_intervals(
    file_path: str,
    locations: dict[str, dict[str, str]],
):
    with open(file_path, newline="", encoding="utf-8") as handle:
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

            start_ip, end_ip = _interval_from_network(network)

            geoname_value = ""
            if geoname_index is not None and geoname_index < len(row):
                geoname_value = row[geoname_index].strip()

            registered_value = ""
            if registered_index is not None and registered_index < len(row):
                registered_value = row[registered_index].strip()

            geoname_id = geoname_value or registered_value
            location = locations.get(geoname_id, {})

            yield {
                "start": start_ip,
                "end": end_ip,
                "continent_code": location.get("continent_code", ""),
                "country_iso_code": location.get("country_iso_code", ""),
                "subdivision": location.get("subdivision", ""),
                "city": location.get("city", ""),
            }


def _iter_asn_intervals(file_path: str):
    with open(file_path, newline="", encoding="utf-8") as handle:
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

            start_ip, end_ip = _interval_from_network(network)

            asn_value = ""
            if asn_index is not None and asn_index < len(row):
                asn_value = row[asn_index].strip()

            organization_value = ""
            if organization_index is not None and organization_index < len(row):
                organization_value = row[organization_index].strip()

            yield {
                "start": start_ip,
                "end": end_ip,
                "asn": asn_value,
                "organization": organization_value,
            }


def _next_interval(interval_iterator) -> dict[str, object] | None:
    try:
        return next(interval_iterator)
    except StopIteration:
        return None


def _segment_to_rows(
    start_ip: int,
    end_ip: int,
    version: int,
    city_data: dict[str, object] | None,
    asn_data: dict[str, object] | None,
) -> list[dict[str, str]]:
    return list(
        _iter_segment_rows(
            start_ip,
            end_ip,
            version,
            city_data,
            asn_data,
        )
    )


def _iter_segment_rows(
    start_ip: int,
    end_ip: int,
    version: int,
    city_data: dict[str, object] | None,
    asn_data: dict[str, object] | None,
):
    if start_ip > end_ip:
        return

    def iter_segments():
        if version == 4:
            yield start_ip, end_ip
            return

        current_start = start_ip
        while current_start <= end_ip:
            bucket = current_start >> IPV6_SCORE_BITS
            bucket_end = ((bucket + 1) << IPV6_SCORE_BITS) - 1
            current_end = min(end_ip, bucket_end)
            yield current_start, current_end
            current_start = current_end + 1

    for segment_start_ip, segment_end_ip in iter_segments():
        segment_start_address = ipaddress.ip_address(segment_start_ip)
        segment_end_address = ipaddress.ip_address(segment_end_ip)

        for network in ipaddress.summarize_address_range(
            segment_start_address,
            segment_end_address,
        ):
            if network.version != version:
                continue

            network_start = int(network.network_address)
            network_end = int(network.broadcast_address)

            row = _blank_output_row()
            row.update(
                {
                    "startip": str(network_start),
                    "endip": str(network_end),
                    "network": str(network),
                }
            )

            if city_data is not None:
                row.update(
                    {
                        "continent_code": str(city_data.get("continent_code", "")),
                        "country_iso_code": str(city_data.get("country_iso_code", "")),
                        "subdivision": str(city_data.get("subdivision", "")),
                        "city": str(city_data.get("city", "")),
                    }
                )

            if asn_data is not None:
                row.update(
                    {
                        "asn": str(asn_data.get("asn", "")),
                        "organization": str(asn_data.get("organization", "")),
                    }
                )

            yield row


def _iter_combined_rows(
    city_intervals,
    asn_intervals,
    version: int,
):
    city_iterator = iter(city_intervals)
    asn_iterator = iter(asn_intervals)
    current_city = _next_interval(city_iterator)
    current_asn = _next_interval(asn_iterator)

    while current_city is not None or current_asn is not None:
        if current_city is None:
            yield from _iter_segment_rows(
                int(current_asn["start"]),
                int(current_asn["end"]),
                version,
                None,
                current_asn,
            )
            current_asn = _next_interval(asn_iterator)
            continue

        if current_asn is None:
            yield from _iter_segment_rows(
                int(current_city["start"]),
                int(current_city["end"]),
                version,
                current_city,
                None,
            )
            current_city = _next_interval(city_iterator)
            continue

        city_start = int(current_city["start"])
        city_end = int(current_city["end"])
        asn_start = int(current_asn["start"])
        asn_end = int(current_asn["end"])

        if city_end < asn_start:
            yield from _iter_segment_rows(city_start, city_end, version, current_city, None)
            current_city = _next_interval(city_iterator)
            continue

        if asn_end < city_start:
            yield from _iter_segment_rows(asn_start, asn_end, version, None, current_asn)
            current_asn = _next_interval(asn_iterator)
            continue

        if city_start < asn_start:
            yield from _iter_segment_rows(
                city_start,
                asn_start - 1,
                version,
                current_city,
                None,
            )
            current_city["start"] = asn_start
            continue

        if asn_start < city_start:
            yield from _iter_segment_rows(
                asn_start,
                city_start - 1,
                version,
                None,
                current_asn,
            )
            current_asn["start"] = city_start
            continue

        overlap_end = min(city_end, asn_end)
        yield from _iter_segment_rows(
            city_start,
            overlap_end,
            version,
            current_city,
            current_asn,
        )

        if city_end == overlap_end:
            current_city = _next_interval(city_iterator)
        else:
            current_city["start"] = overlap_end + 1

        if asn_end == overlap_end:
            current_asn = _next_interval(asn_iterator)
        else:
            current_asn["start"] = overlap_end + 1


def _combine_intervals(
    city_intervals: list[dict[str, object]],
    asn_intervals: list[dict[str, object]],
    version: int,
) -> list[dict[str, str]]:
    return list(_iter_combined_rows(city_intervals, asn_intervals, version))


def _render_output(rows: list[dict[str, str]]) -> str:
    lines = ["|".join(row[field] for field in OUTPUT_FIELDS) for row in rows]
    return "\n".join(lines) + ("\n" if lines else "")


def _render_output_with_stats(rows: list[dict[str, str]]) -> tuple[str, dict[str, int]]:
    lines = []
    output_addresses = 0
    geo_addresses = 0
    asn_output_addresses = 0

    for row in rows:
        lines.append("|".join(row[field] for field in OUTPUT_FIELDS))
        row_addresses = int(row["endip"]) - int(row["startip"]) + 1
        output_addresses += row_addresses
        if row["country_iso_code"]:
            geo_addresses += row_addresses
        if row["asn"]:
            asn_output_addresses += row_addresses

    return "\n".join(lines) + ("\n" if lines else ""), {
        "outputRows": len(rows),
        "outputAddresses": output_addresses,
        "geoAddresses": geo_addresses,
        "asnOutputAddresses": asn_output_addresses,
    }


def _count_addresses(intervals: list[dict[str, object]]) -> int:
    return sum(int(interval["end"]) - int(interval["start"]) + 1 for interval in intervals)


def _count_union_addresses(*interval_groups: list[dict[str, object]]) -> int:
    merged_intervals = []

    for intervals in interval_groups:
        for interval in intervals:
            merged_intervals.append((int(interval["start"]), int(interval["end"])))

    if not merged_intervals:
        return 0

    merged_intervals.sort()
    total = 0
    current_start, current_end = merged_intervals[0]

    for start_ip, end_ip in merged_intervals[1:]:
        if start_ip <= current_end + 1:
            current_end = max(current_end, end_ip)
            continue

        total += current_end - current_start + 1
        current_start, current_end = start_ip, end_ip

    return total + (current_end - current_start + 1)


def _scan_interval_source_file(file_path: str) -> dict[str, int]:
    rows = 0
    addresses = 0

    with open(file_path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header:
            return {"rows": 0, "addresses": 0}

        index = {name: position for position, name in enumerate(header)}
        network_index = index.get("network")
        if network_index is None:
            return {"rows": 0, "addresses": 0}

        for row in reader:
            if network_index >= len(row):
                continue

            network = row[network_index].strip()
            if not network:
                continue

            start_ip, end_ip = _interval_from_network(network)
            rows += 1
            addresses += end_ip - start_ip + 1

    return {
        "rows": rows,
        "addresses": addresses,
    }


def _count_union_addresses_from_sources(city_path: str, asn_path: str) -> int:
    city_iterator = _iter_city_intervals(city_path, {})
    asn_iterator = _iter_asn_intervals(asn_path)
    current_city = _next_interval(city_iterator)
    current_asn = _next_interval(asn_iterator)

    total = 0
    current_start = 0
    current_end = -1

    while current_city is not None or current_asn is not None:
        if current_asn is None:
            next_interval = current_city
            current_city = _next_interval(city_iterator)
        elif current_city is None:
            next_interval = current_asn
            current_asn = _next_interval(asn_iterator)
        elif int(current_city["start"]) <= int(current_asn["start"]):
            next_interval = current_city
            current_city = _next_interval(city_iterator)
        else:
            next_interval = current_asn
            current_asn = _next_interval(asn_iterator)

        start_ip = int(next_interval["start"])
        end_ip = int(next_interval["end"])

        if current_end < current_start:
            current_start = start_ip
            current_end = end_ip
            continue

        if start_ip <= current_end + 1:
            current_end = max(current_end, end_ip)
            continue

        total += current_end - current_start + 1
        current_start = start_ip
        current_end = end_ip

    if current_end >= current_start:
        total += current_end - current_start + 1

    return total


def _build_output_summary_from_counts(
    output_key: str,
    source_city_rows: int,
    source_asn_rows: int,
    city_addresses: int,
    asn_addresses: int,
    union_addresses: int,
    stats: dict[str, int],
) -> dict[str, object]:
    output_addresses = stats["outputAddresses"]
    geo_addresses = stats["geoAddresses"]
    asn_output_addresses = stats["asnOutputAddresses"]

    return {
        "event": "processed_output_summary",
        "output": output_key,
        "sourceCityRows": source_city_rows,
        "sourceAsnRows": source_asn_rows,
        "outputRows": stats["outputRows"],
        "cityAddresses": city_addresses,
        "asnAddresses": asn_addresses,
        "unionAddresses": union_addresses,
        "outputAddresses": output_addresses,
        "geoAddresses": geo_addresses,
        "asnOutputAddresses": asn_output_addresses,
        "cityCoverageComplete": geo_addresses == city_addresses,
        "asnCoverageComplete": asn_output_addresses == asn_addresses,
        "unionCoverageComplete": output_addresses == union_addresses,
    }


def _build_output_summary(
    output_key: str,
    city_intervals: list[dict[str, object]],
    asn_intervals: list[dict[str, object]],
    stats: dict[str, int],
) -> dict[str, object]:
    return _build_output_summary_from_counts(
        output_key=output_key,
        source_city_rows=len(city_intervals),
        source_asn_rows=len(asn_intervals),
        city_addresses=_count_addresses(city_intervals),
        asn_addresses=_count_addresses(asn_intervals),
        union_addresses=_count_union_addresses(city_intervals, asn_intervals),
        stats=stats,
    )


def build_output_artifacts(directory: str) -> dict[str, dict[str, object]]:
    return build_output_artifacts_for_families(directory, sorted(FAMILY_CONFIG.keys()))


def build_output_artifacts_for_families(
    directory: str,
    families: list[int],
) -> dict[str, dict[str, object]]:
    locations = _load_locations(os.path.join(directory, "GeoLite2-City-Locations-en.csv"))
    artifacts = {}

    for version in families:
        config = FAMILY_CONFIG[version]
        city_intervals = _read_city_intervals(
            os.path.join(directory, config["city"]),
            locations,
        )
        asn_intervals = _read_asn_intervals(os.path.join(directory, config["asn"]))
        rows = _combine_intervals(city_intervals, asn_intervals, version=version)
        body, stats = _render_output_with_stats(rows)
        artifacts[config["output"]] = {
            "body": body,
            "summary": _build_output_summary(
                config["output"],
                city_intervals,
                asn_intervals,
                stats,
            ),
        }

    return artifacts


def _build_outputs_from_directory(directory: str) -> dict[str, str]:
    return {
        output_key: artifact["body"]
        for output_key, artifact in build_output_artifacts(directory).items()
    }


def build_outputs_from_directory(directory: str) -> dict[str, str]:
    return _build_outputs_from_directory(directory)


def combine_intervals(
    city_intervals: list[dict[str, object]],
    asn_intervals: list[dict[str, object]],
    version: int,
) -> list[dict[str, str]]:
    return _combine_intervals(city_intervals, asn_intervals, version)


def _download_sources(s3_client, bucket_name: str, directory: str) -> None:
    _download_named_sources(s3_client, bucket_name, sorted(SOURCE_FILES), directory)


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


def _family_source_files(family: int) -> list[str]:
    config = FAMILY_CONFIG[family]
    return [
        "GeoLite2-City-Locations-en.csv",
        config["city"],
        config["asn"],
    ]


def _missing_source_files(s3_client, bucket_name: str) -> list[str]:
    missing = []

    for file_name in sorted(SOURCE_FILES):
        try:
            s3_client.head_object(Bucket=bucket_name, Key=file_name)
        except Exception as exc:
            response = getattr(exc, "response", {})
            error = response.get("Error", {})
            error_code = str(error.get("Code", ""))

            if error_code in {"404", "NoSuchKey", "NotFound"}:
                missing.append(file_name)
                continue

            raise

    return missing


def _source_metadata(
    s3_client,
    bucket_name: str,
) -> tuple[list[str], dict[str, str]]:
    missing = []
    signature_parts = {}

    for file_name in sorted(SOURCE_FILES):
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


def _family_signature(source_metadata: dict[str, str], family: int) -> str:
    parts = []
    for file_name in _family_source_files(family):
        parts.append(f"{file_name}:{source_metadata.get(file_name, '')}")
    return "|".join(parts)


def _families_from_relevant_records(relevant_records: list[dict[str, str]]) -> list[int]:
    families = set()
    for record in relevant_records:
        key = record["key"]
        if "IPv4" in key:
            families.add(4)
            continue
        if "IPv6" in key:
            families.add(6)

    if not families:
        return sorted(FAMILY_CONFIG.keys())
    return sorted(families)


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


def _extract_family_jobs(event) -> list[dict[str, Any]]:
    jobs = []

    for record in event.get("Records", []):
        if "body" not in record:
            continue

        payload = json.loads(record.get("body", "{}"))
        if payload.get("jobType") != JOB_TYPE_FAMILY_BUILD:
            continue

        family = payload.get("family")
        if family not in FAMILY_CONFIG:
            continue

        jobs.append(payload)

    return jobs


def _enqueue_family_jobs(
    sqs_client,
    queue_url: str,
    family_signatures: dict[int, str],
    families: list[int],
) -> list[int]:
    queued_families = []

    for family in families:
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(
                {
                    "jobType": JOB_TYPE_FAMILY_BUILD,
                    "family": family,
                    "sourceSignature": family_signatures[family],
                },
                sort_keys=True,
            ),
        )
        queued_families.append(family)

    return queued_families


def _process_family_job(
    s3_client,
    download_bucket_name: str,
    processed_bucket_name: str,
    family: int,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        _download_named_sources(
            s3_client,
            download_bucket_name,
            _family_source_files(family),
            directory,
        )
        config = FAMILY_CONFIG[family]
        output_key = config["output"]
        city_path = os.path.join(directory, config["city"])
        asn_path = os.path.join(directory, config["asn"])
        locations_path = os.path.join(directory, "GeoLite2-City-Locations-en.csv")
        referenced_geoname_ids = _collect_city_geoname_ids(city_path)
        locations = _load_locations_subset(locations_path, referenced_geoname_ids)
        city_scan = _scan_interval_source_file(city_path)
        asn_scan = _scan_interval_source_file(asn_path)
        union_addresses = _count_union_addresses_from_sources(city_path, asn_path)

        output_path = os.path.join(directory, output_key)
        output_rows = 0
        output_addresses = 0
        geo_addresses = 0
        asn_output_addresses = 0

        with open(output_path, "w", encoding="utf-8", newline="") as output_handle:
            for row in _iter_combined_rows(
                _iter_city_intervals(city_path, locations),
                _iter_asn_intervals(asn_path),
                family,
            ):
                output_handle.write("|".join(row[field] for field in OUTPUT_FIELDS))
                output_handle.write("\n")

                row_addresses = int(row["endip"]) - int(row["startip"]) + 1
                output_rows += 1
                output_addresses += row_addresses
                if row["country_iso_code"]:
                    geo_addresses += row_addresses
                if row["asn"]:
                    asn_output_addresses += row_addresses

        summary = _build_output_summary_from_counts(
            output_key=output_key,
            source_city_rows=city_scan["rows"],
            source_asn_rows=asn_scan["rows"],
            city_addresses=city_scan["addresses"],
            asn_addresses=asn_scan["addresses"],
            union_addresses=union_addresses,
            stats={
                "outputRows": output_rows,
                "outputAddresses": output_addresses,
                "geoAddresses": geo_addresses,
                "asnOutputAddresses": asn_output_addresses,
            },
        )

        s3_client.upload_file(
            output_path,
            processed_bucket_name,
            output_key,
            ExtraArgs={"ContentType": "text/plain"},
        )
        print(json.dumps(summary, sort_keys=True))

    return {
        "family": family,
        "output": output_key,
    }


def handler(event, context):
    started_at = time.perf_counter()
    request_id = getattr(context, "aws_request_id", "")

    download_bucket_name = os.environ["DOWNLOAD_BUCKET_NAME"]
    processed_bucket_name = os.environ["PROCESSED_BUCKET_NAME"]
    relevant_records = _extract_relevant_records(event)
    family_jobs = _extract_family_jobs(event)

    _log_event(
        "processed_run_start",
        requestId=request_id,
        downloadBucket=download_bucket_name,
        processedBucket=processed_bucket_name,
        familyJobCount=len(family_jobs),
        relevantRecordCount=len(relevant_records),
        relevantKeys=sorted({record["key"] for record in relevant_records}),
    )

    s3_client = _boto3_client("s3")

    if family_jobs:
        processed_jobs = []
        for family_job in family_jobs:
            family = int(family_job["family"])
            processed_jobs.append(
                _process_family_job(
                    s3_client,
                    download_bucket_name,
                    processed_bucket_name,
                    family,
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

    missing_files, source_metadata = _source_metadata(s3_client, download_bucket_name)

    if missing_files:
        print(
            "Skipping rebuild until all required source files exist: "
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

    impacted_families = _families_from_relevant_records(relevant_records)
    family_signatures = {
        family: _family_signature(source_metadata, family)
        for family in impacted_families
    }
    families_to_queue = [
        family
        for family in impacted_families
        if family_signatures[family]
        != _RUNTIME_STATE["last_processed_family_signatures"].get(family, "")
    ]

    if not families_to_queue:
        print("Skipping rebuild because impacted family source files are unchanged")
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
            impactedFamilies=impacted_families,
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
            "impactedFamilies": impacted_families,
        }

    queue_url = os.environ["PROCESS_QUEUE_URL"]
    sqs_client = _boto3_client("sqs")
    queued_families = _enqueue_family_jobs(
        sqs_client,
        queue_url,
        family_signatures,
        families_to_queue,
    )

    for family in queued_families:
        _RUNTIME_STATE["last_processed_family_signatures"][family] = family_signatures[
            family
        ]

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    _log_event(
        "processed_run_result",
        requestId=request_id,
        skipped=True,
        skipReason="queued_family_jobs",
        outputs=[],
        missingSourceFiles=[],
        durationMs=duration_ms,
        queuedFamilies=queued_families,
        mode="coordinator",
        impactedFamilies=impacted_families,
    )

    return {
        "statusCode": 200,
        "downloadBucket": download_bucket_name,
        "processedBucket": processed_bucket_name,
        "processed": relevant_records,
        "outputs": [],
        "skipped": True,
        "skipReason": "queued_family_jobs",
        "missingSourceFiles": [],
        "queuedFamilies": queued_families,
        "mode": "coordinator",
        "impactedFamilies": impacted_families,
    }

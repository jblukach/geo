#!/usr/bin/env python3
"""Simple load test for geo-search endpoint.

Example:
  python scripts/load_test_search.py \
    --url https://example.execute-api.us-east-2.amazonaws.com/geo \
    --requests 200 \
    --ips-per-request 300 \
    --concurrency 20 \
    --ipv6-ratio 1.0
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import secrets
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load test geo-search bulk endpoint")
    parser.add_argument("--url", required=True, help="Full search endpoint URL")
    parser.add_argument("--requests", type=int, default=100, help="Total request count")
    parser.add_argument("--ips-per-request", type=int, default=300, help="IPs per request")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent workers")
    parser.add_argument("--timeout-seconds", type=float, default=28.0, help="HTTP request timeout")
    parser.add_argument("--ipv6-ratio", type=float, default=0.5, help="0.0 to 1.0 proportion of IPv6 IPs")
    parser.add_argument("--seed", type=int, default=42, help="Unused; retained for backward compatibility")
    parser.add_argument("--api-key", default="", help="Optional x-api-key header")
    return parser


def _random_ipv4(rng: secrets.SystemRandom) -> str:
    value = rng.getrandbits(32)
    return str(ipaddress.IPv4Address(value))


def _random_ipv6(rng: secrets.SystemRandom) -> str:
    value = rng.getrandbits(128)
    return str(ipaddress.IPv6Address(value))


def build_ip_batch(rng: secrets.SystemRandom, count: int, ipv6_ratio: float) -> list[str]:
    ips: list[str] = []
    for _ in range(count):
        if rng.random() < ipv6_ratio:
            ips.append(_random_ipv6(rng))
        else:
            ips.append(_random_ipv4(rng))
    return ips


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = (len(sorted_values) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit("--url must use http or https")
    if not parsed.netloc:
        raise SystemExit("--url must include a valid host")


def do_request(url: str, timeout_seconds: float, api_key: str, payload: dict[str, object]) -> tuple[int, float, int]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"content-type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key

    request = urllib.request.Request(url=url, method="POST", data=body, headers=headers)

    start = time.perf_counter()
    try:
        # URL scheme/host are validated in main() via validate_url().
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            status_code = response.getcode()
            response_body = response.read()
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        response_body = exc.read()
    except urllib.error.URLError:
        duration_ms = (time.perf_counter() - start) * 1000.0
        return (0, duration_ms, 0)

    duration_ms = (time.perf_counter() - start) * 1000.0
    return (status_code, duration_ms, len(response_body))


def main() -> int:
    args = build_parser().parse_args()
    validate_url(args.url)

    if args.requests <= 0:
        raise SystemExit("--requests must be greater than 0")
    if args.ips_per_request <= 0:
        raise SystemExit("--ips-per-request must be greater than 0")
    if args.concurrency <= 0:
        raise SystemExit("--concurrency must be greater than 0")
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be greater than 0")
    if args.ipv6_ratio < 0.0 or args.ipv6_ratio > 1.0:
        raise SystemExit("--ipv6-ratio must be between 0.0 and 1.0")

    del args.seed
    rng = secrets.SystemRandom()
    batches = [build_ip_batch(rng, args.ips_per_request, args.ipv6_ratio) for _ in range(args.requests)]

    status_counts: dict[int, int] = {}
    latencies_ms: list[float] = []
    response_sizes: list[int] = []

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(
                do_request,
                args.url,
                args.timeout_seconds,
                args.api_key,
                {"ips": batch},
            )
            for batch in batches
        ]

        for future in as_completed(futures):
            status_code, latency_ms, response_size = future.result()
            status_counts[status_code] = status_counts.get(status_code, 0) + 1
            latencies_ms.append(latency_ms)
            response_sizes.append(response_size)

    elapsed_seconds = max(time.perf_counter() - started, 0.001)
    latencies_ms.sort()

    success_count = sum(count for code, count in status_counts.items() if 200 <= code < 300)
    error_count = args.requests - success_count

    print("Load Test Summary")
    print(f"requests={args.requests} concurrency={args.concurrency} ipsPerRequest={args.ips_per_request} ipv6Ratio={args.ipv6_ratio}")
    print(f"success={success_count} errors={error_count} rps={args.requests / elapsed_seconds:.2f}")
    print("statusCounts=" + json.dumps(status_counts, sort_keys=True))
    print(
        "latencyMs="
        + json.dumps(
            {
                "min": round(latencies_ms[0], 2),
                "p50": round(percentile(latencies_ms, 0.50), 2),
                "p95": round(percentile(latencies_ms, 0.95), 2),
                "p99": round(percentile(latencies_ms, 0.99), 2),
                "max": round(latencies_ms[-1], 2),
                "avg": round(statistics.fmean(latencies_ms), 2),
            },
            sort_keys=True,
        )
    )
    print(
        "responseBytes="
        + json.dumps(
            {
                "min": min(response_sizes),
                "p50": round(percentile(sorted(response_sizes), 0.50), 2),
                "p95": round(percentile(sorted(response_sizes), 0.95), 2),
                "max": max(response_sizes),
                "avg": round(statistics.fmean(response_sizes), 2),
            },
            sort_keys=True,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

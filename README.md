# geo

AWS CDK project for downloading GeoLite2 source data, transforming it into Valkey-ready range records, and serving low-latency IP lookups from Lambda.

## What this repository deploys

This app creates four stack groups:

1. GeoStack
2. GeoNetworkStack
3. GeoProcessStack
4. GeoSearchStack

At a high level:

1. Download jobs place source CSV files in an S3 download bucket.
2. S3 object-created events fan into SQS and trigger geolite2-process.
3. geolite2-process writes processed output artifacts and loads ASN and City ranges into Valkey sorted sets.
4. geo-search reads from those Valkey sorted sets and returns ASN and geo metadata for one or many IPs.

## Architecture notes

1. Runtime is Python 3.13 on Lambda arm64.
2. Network stack uses isolated subnets and an ElastiCache Serverless for Valkey endpoint.
3. Process and search Lambdas run inside the VPC and share a security-group-to-security-group rule for Valkey port 6379.
4. Process pipeline uses an SQS queue with DLQ for resilient retries.
5. Valkey release management is set-name based; there is no release label environment variable.

## Repository layout

1. geo/: CDK stack definitions.
2. download/: source download Lambdas.
3. process/: transform and Valkey load Lambda.
4. search/: lookup Lambda.
5. scripts/: operator tools, including load test utility.
6. docs/: operational runbooks.
7. tests/: unit tests for process/search and stack wiring.

## Local setup

Prerequisites:

1. Python 3.13.
2. Node.js and AWS CDK CLI.
3. AWS credentials with permissions for CDK deploy.

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run tests:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

Synthesize stacks:

```bash
cdk synth
```

Deploy all stacks:

```bash
cdk deploy --all
```

Deploy only process/search updates:

```bash
cdk deploy GeoProcessStack GeoSearchStack
```

## Key configuration

Source of defaults: config.py

Process Lambda environment:

1. VALKEY_ENDPOINT: required cache endpoint.
2. VALKEY_PORT: cache port, default 6379.
3. VALKEY_TLS: true or false, default true.
4. VALKEY_SORTED_SET_BATCH_SIZE: batched ZADD write size, default 5000.
5. VALKEY_ASN_V4_SET_NAME: default asn_v4_ranges.
6. VALKEY_ASN_V6_SET_NAME: default asn_v6_ranges.
7. VALKEY_CITY_V4_SET_NAME: default city_v4_ranges.
8. VALKEY_CITY_V6_SET_NAME: default city_v6_ranges.
9. VALKEY_LAST_UPDATED_ASN_KEY: default geo:last_updated:asn.
10. VALKEY_LAST_UPDATED_CITY_KEY: default geo:last_updated:city.
11. VALKEY_MAX_CONNECTIONS: connection pool size, default 8.

Search Lambda environment:

1. VALKEY_ENDPOINT, VALKEY_PORT, VALKEY_TLS.
2. VALKEY_ASN_V4_SET_NAME, VALKEY_ASN_V6_SET_NAME, VALKEY_CITY_V4_SET_NAME, VALKEY_CITY_V6_SET_NAME.
3. VALKEY_LAST_UPDATED_ASN_KEY, VALKEY_LAST_UPDATED_CITY_KEY.
4. MAX_IPS_PER_REQUEST: default 300.
5. MAX_REQUEST_BODY_BYTES: default 262144.
6. MIN_REMAINING_TIME_MS: default 1500.

## Search API behavior

Input forms supported:

1. Query parameter ip (single or comma-separated).
2. JSON body with ip, ips, ipAddress, or query.
3. Path input patterns under /geo.
4. GET /geo fallback to caller source IP when explicit IP input is not provided.

Response behavior:

1. Returns status 200 with per-entry results, including per-IP validation errors.
2. Preserves original input order in results.
3. Deduplicates valid IP lookups internally to reduce Valkey query volume.
4. Returns 400 for empty input or oversized IP count.
5. Returns 413 when request body exceeds MAX_REQUEST_BODY_BYTES.
6. Returns 503 when Lambda remaining budget is below MIN_REMAINING_TIME_MS.

## Load test utility

```bash
python scripts/load_test_search.py \
  --url https://<api-id>.execute-api.us-east-2.amazonaws.com/geo \
  --requests 200 \
  --ips-per-request 300 \
  --concurrency 20 \
  --ipv6-ratio 1.0
```

Suggested service goals:

| Metric | Target |
| --- | --- |
| Success rate (2xx) | >= 99.9% |
| p95 latency | <= 3.0s |
| p99 latency | <= 6.0s |
| Error rate (5xx) | < 0.1% |

## Production readiness checklist

Before promoting `geo-search` changes, verify:

1. Input-path unit tests pass for query/body/path inputs, URL-encoded IPv6 path segments, and invalid IP handling.
2. Exact-limit bulk request test passes for `MAX_IPS_PER_REQUEST=300`.
3. Load-test results are captured for 300-IP POST requests at target concurrency.
4. 2xx success rate, p95, p99, and 5xx error rate are within service goals.
5. Response includes expected metadata fields (`attribution`, `geolite2-asn.csv`, `geolite2-city.csv`).

Current validated baseline (2026-06-20, production endpoint):

1. 300-IP POST at concurrency 10: 100% success, p95 about 1.40s, p99 about 1.46s.
2. 300-IP POST at concurrency 12: saturation observed (503 responses).
3. Practical starting envelope: keep sustained bulk traffic at or below concurrency 10 unless new load tests prove otherwise.

Deployment tuning notes:

1. Search Lambda memory is set to 1024 MB (`SEARCH_LAMBDA_MEMORY_SIZE_MB`).
2. Reserved concurrency is optional (`SEARCH_LAMBDA_RESERVED_CONCURRENCY`). Default is unset to avoid account-level unreserved concurrency floor conflicts.
3. If you set reserved concurrency, ensure the account retains the required unreserved minimum before deploying.

## Security model

1. Process and search run in private isolated subnets.
2. Valkey allows inbound 6379 only from the Lambda security group.
3. TLS to Valkey is enabled by default via VALKEY_TLS=true.
4. Process queue and DLQ enforce SSL.
5. Valkey authentication token is not used in this isolated-network deployment model.

## Release operations

Use the release runbook for full cutover and rollback procedures:

1. [docs/valkey-release-runbook.md](docs/valkey-release-runbook.md)

Short version:

1. Set new release-specific sorted set names in config.py.
2. Deploy GeoProcessStack.
3. Trigger processing by uploading GeoLite2 source files.
4. Validate load completion in logs.
5. Deploy readers with matching set names.
6. Keep previous set names available during rollback window.

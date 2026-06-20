# Valkey Release Runbook

This runbook covers a safe release workflow for loading GeoLite2 processed data into ElastiCache Serverless for Valkey.

## Goal

Use release-scoped sorted set names so you can:

1. Load a new release without touching currently served data.
2. Validate new data before cutover.
3. Roll back quickly by switching set names.
4. Delete old release data after a safety window.

This project does not use a dedicated release label variable. Releases are isolated by explicitly setting dataset set names:

- VALKEY_ASN_V4_SET_NAME
- VALKEY_ASN_V6_SET_NAME
- VALKEY_CITY_V4_SET_NAME
- VALKEY_CITY_V6_SET_NAME

## Inputs You Need Before Starting

1. Release suffix, for example r20260620.
2. Four new target set names for ASN and City in both IPv4 and IPv6.
3. AWS account and region for deployment.
4. Confirmation that both GeoNetworkStack and GeoProcessStack are healthy.

## Prerequisites

1. GeoNetworkStack and GeoProcessStack are deployed.
2. A Valkey serverless cache endpoint exists and is reachable from the process Lambda.
3. Process Lambda receives VALKEY_ENDPOINT and VALKEY_PORT from deployment.
4. Valkey authentication secret is not required for this isolated-network deployment model.
5. Process queue and DLQ are healthy (no sustained DLQ growth before release).

Endpoint source:

- Lambda environment variable `VALKEY_ENDPOINT`.

## Recommended Set Naming

Use a sortable suffix and keep dataset/IP version prefix stable:

- asn_v4_ranges_rYYYYMMDD
- asn_v6_ranges_rYYYYMMDD
- city_v4_ranges_rYYYYMMDD
- city_v6_ranges_rYYYYMMDD

Examples:

- asn_v4_ranges_r20260619
- city_v6_ranges_r20260619

## 1. Choose New Release Set Names

Pick a release suffix and prepare four new names (ASN/City x IPv4/IPv6).

Example release set:

1. asn_v4_ranges_r20260620
2. asn_v6_ranges_r20260620
3. city_v4_ranges_r20260620
4. city_v6_ranges_r20260620

## 2. Update Config And Deploy Processor

Update config.py values:

- VALKEY_ASN_V4_SET_NAME
- VALKEY_ASN_V6_SET_NAME
- VALKEY_CITY_V4_SET_NAME
- VALKEY_CITY_V6_SET_NAME

Deploy GeoProcessStack so the process Lambda receives the new names.

Example:

```bash
cdk deploy GeoProcessStack
```

After deploy, new process runs write to the release-specific sets you configured.

Optional verification:

1. Confirm geolite2-process environment has the expected four VALKEY_*_SET_NAME values.

## 3. Trigger Processing For Source Files

The process lambda is normally triggered by source file uploads into the download bucket.

If needed, trigger by uploading updated source files:

- GeoLite2-ASN-Blocks-IPv4.csv
- GeoLite2-ASN-Blocks-IPv6.csv
- GeoLite2-City-Blocks-IPv4.csv
- GeoLite2-City-Blocks-IPv6.csv

Operational expectation:

1. One S3 upload should enqueue one process job.
2. Retries are handled by SQS visibility timeout and DLQ policy.

## 4. Validate Load Completion

Confirm process logs include:

- processed_run_result with mode=worker
- valkey_load_summary with loadedRows and setCount

Example CloudWatch Logs Insights query:

```sql
fields @timestamp, @message
| filter @message like /valkey_load_summary|processed_run_result/
| sort @timestamp desc
| limit 100
```

Recommended validation checks:

1. loadedRows is non-zero.
2. setCount is 4 for a full ASN+City load.
3. outputs include the expected processed TXT artifacts.
4. no sustained batch failures or malformed row parse warnings.

If validation fails:

1. Do not cut over readers.
2. Fix source/config issue and re-run processing into the same release set names.

## 5. Cut Over Readers To New Sets

Deploy reader services with the same four VALKEY_* set-name values used during load.

For bulk lookup readers (`geo-search`), also confirm `MAX_IPS_PER_REQUEST` is set to a value your workload and cache capacity can safely handle.
Recommended starting point is `300` and then tune upward only after observing p95/p99 search latency under production-like load.
Recommended companion settings are `MAX_REQUEST_BODY_BYTES=262144` and `MIN_REMAINING_TIME_MS=1500`.

Example load-test command:

```bash
python scripts/load_test_search.py \
	--url https://<api-id>.execute-api.us-east-2.amazonaws.com/geo \
	--requests 200 \
	--ips-per-request 300 \
	--concurrency 20 \
	--ipv6-ratio 1.0
```

Suggested cutover gates for bulk reader traffic:

1. 2xx success rate is at least 99.9%.
2. p95 latency is 3 seconds or less.
3. p99 latency is 6 seconds or less.
4. 5xx error rate remains below 0.1%.

Observed baseline (2026-06-20):

1. 300-IP POST requests were stable through concurrency 10.
2. Concurrency 12 introduced 503 saturation responses.
3. Re-validate these limits after any major Lambda, networking, or Valkey capacity change.

Lambda concurrency configuration note:

1. Reserved concurrency does not add direct cost, but it consumes account concurrency allocation.
2. AWS enforces a minimum unreserved concurrency pool; overly high reserved settings can fail deployment.
3. Keep `SEARCH_LAMBDA_RESERVED_CONCURRENCY` unset by default unless your account quota and unreserved headroom are confirmed.

Suggested additional gates:

1. Search responses include geolite2-asn.csv and geolite2-city.csv timestamps.
2. Invalid IP inputs return per-entry errors without failing the full request.
3. No timeout trend in geo-search CloudWatch metrics after limited canary traffic.

Example:

```bash
# deploy reader stack or service here
```

After cutover, lookups read from the new release sets.

Post-cutover check window:

1. Run at least 15 to 30 minutes of production-like traffic observation before retiring old sets.

## 6. Rollback

To roll back, redeploy readers with the previous four set names.

Example:

```bash
# deploy reader stack or service here
```

No cache reload is required if previous data is still retained in alternate sets.

Rollback trigger examples:

1. sustained p99 regression above release SLO.
2. elevated 5xx not explained by upstream dependencies.
3. data quality issues in ASN or City fields.

## 7. Retire Old Release Data

After your safety window (for example, 24 to 72 hours), delete old sets.

Recommended policy:

1. Keep only Active and Previous releases.
2. Remove any older release namespaces.

Before deleting old sets:

1. confirm rollback window is closed.
2. confirm no reader configuration still references old set names.

## Operational Notes

1. Do not flush the entire cache for normal releases.
2. Keep TTL disabled on active canonical data.
3. Use separate set names for safe bulk reloads and fast rollback.
4. Keep naming format predictable so operational tooling can identify active and stale releases.
5. Keep Valkey access inside private isolated subnets and security-group allowlists only.
6. Keep MAX_IPS_PER_REQUEST conservative until cache latency and Lambda duration data are stable after each release.

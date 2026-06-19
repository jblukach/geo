# Momento Release Runbook

This runbook covers a safe release workflow for loading GeoLite2 processed data into Momento using release namespaces.

## Goal

Use release-scoped set names so you can:

1. Load a new release without touching currently served data.
2. Validate new data before cutover.
3. Roll back quickly by switching release label.
4. Delete old release data after a safety window.

Set names are generated as:

- Without release: geo:asn:<shard> and geo:city:<shard>
- With release: geo:<release>:asn:<shard> and geo:<release>:city:<shard>

## Prerequisites

1. GeoProcessStack deployed with Momento env vars and layer.
2. credentials secret contains MOMENTO and, for v2 API keys, MOMENTO_ENDPOINT.
3. Process lambda can read credentials secret.
4. A cache exists in Momento (default in this repo is geo).

For v2 API keys, endpoint can come from either:

- Lambda env var MOMENTO_ENDPOINT
- Secret field MOMENTO_ENDPOINT (or the field referenced by MOMENTO_ENDPOINT_SECRET_KEY)

## Recommended Release Naming

Use a sortable release label:

- rYYYYMMDD
- rYYYYMMDD-HHMM

Examples:

- r20260619
- r20260619-1530

## 1. Set Release Label And Deploy Processor

Export the release label before CDK deploy so GeoProcessStack injects it into MOMENTO_RELEASE.

```bash
export MOMENTO_RELEASE=r20260619
cdk deploy GeoProcessStack
```

This causes process runs to write to release-scoped sets, for example:

- geo:r20260619:asn:v4:xxxx
- geo:r20260619:city:v6:xxxx

## 2. Trigger Processing For Source Files

The process lambda is normally triggered by source file uploads into the download bucket.

If needed, trigger by uploading updated source files:

- GeoLite2-ASN-Blocks-IPv4.csv
- GeoLite2-ASN-Blocks-IPv6.csv
- GeoLite2-City-Blocks-IPv4.csv
- GeoLite2-City-Blocks-IPv6.csv

## 3. Validate Load Completion

Confirm process logs include:

- processed_run_result with mode=worker
- momento_load_summary with loadedRows and setCount

Example CloudWatch Logs Insights query:

```sql
fields @timestamp, @message
| filter @message like /momento_load_summary|processed_run_result/
| sort @timestamp desc
| limit 100
```

## 4. Cut Over Readers To New Release

Deploy reader services with the same MOMENTO_RELEASE value used during load.

Example:

```bash
export MOMENTO_RELEASE=r20260619
# deploy reader stack or service here
```

After cutover, lookups will read from geo:r20260619:* sets.

## 5. Rollback

To roll back, redeploy readers with the previous release value.

Example:

```bash
export MOMENTO_RELEASE=r20260612
# deploy reader stack or service here
```

No cache reload is required if previous release data is still retained.

## 6. Retire Old Release Data

After your safety window (for example, 24 to 72 hours), delete old release sets.

Recommended policy:

1. Keep only Active and Previous releases.
2. Remove any older release namespaces.

## Operational Notes

1. Do not flush the entire cache for normal releases.
2. Keep TTL disabled on active canonical data.
3. Use release namespaces for safe bulk reloads and fast rollback.
4. Keep MOMENTO_SET_PREFIX stable unless you intentionally migrate keyspace layout.

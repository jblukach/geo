# geo

GeoLite2 download and processing pipeline with Valkey-ready output artifacts.

The processor loads GeoLite2 ranges into AWS ElastiCache Serverless for Valkey
using Redis sorted sets and batched writes.

## Release Runbook

Use the Valkey runbook for rollout and cutover steps:

- [docs/valkey-release-runbook.md](docs/valkey-release-runbook.md)

Release workflow summary:

1. Set release-specific sorted set names in `config.py` (`VALKEY_ASN_V4_SET_NAME`, `VALKEY_ASN_V6_SET_NAME`, `VALKEY_CITY_V4_SET_NAME`, `VALKEY_CITY_V6_SET_NAME`).
2. Deploy `GeoProcessStack`.
3. Process source CSV uploads.
4. Cut over readers to the same four set names.
5. Keep previous release sets for rollback, then retire old sets.

Process Lambda configuration:

- `VALKEY_ENDPOINT`: ElastiCache Serverless for Valkey endpoint.
- `VALKEY_PORT`: cache port. Default: `6379`.
- `VALKEY_TLS`: enable TLS (`true`/`false`). Default: `true`.
- `VALKEY_SORTED_SET_BATCH_SIZE`: number of rows buffered before a batched `ZADD`. Default: `5000`.
- `VALKEY_ASN_V4_SET_NAME`: ASN sorted set for IPv4 ranges. Default: `asn_v4_ranges`.
- `VALKEY_ASN_V6_SET_NAME`: ASN sorted set for IPv6 ranges. Default: `asn_v6_ranges`.
- `VALKEY_CITY_V4_SET_NAME`: City sorted set for IPv4 ranges. Default: `city_v4_ranges`.
- `VALKEY_CITY_V6_SET_NAME`: City sorted set for IPv6 ranges. Default: `city_v6_ranges`.
- `VALKEY_MAX_CONNECTIONS`: redis connection pool size. Default: `8`.

Search Lambda configuration:

- `VALKEY_ENDPOINT`: ElastiCache Serverless for Valkey endpoint.
- `VALKEY_PORT`: cache port. Default: `6379`.
- `VALKEY_TLS`: enable TLS (`true`/`false`). Default: `true`.
- `MAX_IPS_PER_REQUEST`: maximum IP values accepted per request. Default: `300`.
- `MAX_REQUEST_BODY_BYTES`: maximum accepted request body size in bytes. Default: `262144`.
- `MIN_REMAINING_TIME_MS`: minimum required Lambda budget during processing; requests return `503` when budget is too low. Default: `1500`.

Bulk search behavior:

1. Supports single-IP and bulk inputs (`ip`, `ips`, query string, or JSON body).
2. Preserves input order in `results` and returns per-IP errors for invalid values.
3. Deduplicates valid IP lookups internally to reduce Valkey query load.
4. Enforces `MAX_IPS_PER_REQUEST` to protect Lambda/Valkey from oversized requests.
5. Rejects oversized request bodies with `413`.
6. Returns `503` before timeout if remaining execution budget is too low.

Timeout and sizing guidance:

1. Search currently performs two Valkey reads per unique valid IP (ASN + City).
2. Approximate lookup time envelope is `2 * unique_ips * p95_valkey_rtt`.
3. With `300` unique IPs, rough lookup time is ~0.6s at 1 ms RTT, ~3s at 5 ms RTT, and ~6s at 10 ms RTT.
4. Keep `MAX_IPS_PER_REQUEST` conservative enough to stay well below API Gateway's 29-second timeout after JSON serialization and network transfer.

Load testing:

```bash
python scripts/load_test_search.py \
	--url https://<api-id>.execute-api.us-east-2.amazonaws.com/geo \
	--requests 200 \
	--ips-per-request 300 \
	--concurrency 20 \
	--ipv6-ratio 1.0
```

Suggested SLO targets for bulk search:

| Metric | Target |
| --- | --- |
| Success rate (2xx) | >= 99.9% |
| p95 latency | <= 3.0s |
| p99 latency | <= 6.0s |
| Error rate (5xx) | < 0.1% |

Additional production hardening recommendations:

1. Configure API Gateway throttling and usage plans to limit abusive callers.
2. Set Lambda reserved concurrency for `geo-search` to protect Valkey from sudden traffic spikes.
3. Add CloudWatch alarms on 5xx count, p95 duration, and timeout count.
4. Track and alert on `search_request_summary` log fields (`durationMs`, `invalidCount`, `requestedCount`, `uniqueValidCount`).
5. Keep `MAX_IPS_PER_REQUEST` tuned by observed p95/p99 latency, not static defaults.

Valkey auth model:

- On the isolated VPC deployment model, Valkey access is network-isolated and does not require an auth secret token.
- Endpoint and port are provided directly by the network stack.

Security validation checklist:

1. `geo-search` and `geolite2-process` run in private isolated subnets.
2. Valkey security group only allows inbound `6379/tcp` from the Lambda security group.
3. Lambda-to-Valkey traffic uses TLS (`VALKEY_TLS=true` by default).
4. SQS queues enforce TLS (`enforce_ssl=True`) and include a DLQ.
5. No Valkey secret token dependency in process/search runtime.

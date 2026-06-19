# geo

GeoLite2 download and processing pipeline with Momento-ready output artifacts.

The processor writes each GeoLite2 source into its own Momento cache and loads
sorted-set data in batches for better throughput and lower log noise.

## Momento Release Runbook

Use the release runbook for blue/green cache updates with `MOMENTO_RELEASE`:

- [docs/momento-release-runbook.md](docs/momento-release-runbook.md)

Process Lambda configuration:

- `MOMENTO_ENDPOINT`: Momento cache endpoint used by the Lambda.
- `MOMENTO_CACHE_NAMES_BY_SOURCE`: JSON mapping from source CSV file name to Momento cache name.
- `MOMENTO_SORTED_SET_BATCH_SIZE`: number of rows buffered before a bulk sorted-set write is sent to Momento. Default: `250`.
- `MOMENTO_SECRET_NAME`
- `MOMENTO_SECRET_KEY`
- `MOMENTO_SET_PREFIX`
- `MOMENTO_RELEASE`

Default source-to-cache mapping:

- `GeoLite2-ASN-Blocks-IPv4.csv` -> `GeoLite2-ASN-Blocks-IPv4`
- `GeoLite2-ASN-Blocks-IPv6.csv` -> `GeoLite2-ASN-Blocks-IPv6`
- `GeoLite2-City-Blocks-IPv4.csv` -> `GeoLite2-City-Blocks-IPv4`
- `GeoLite2-City-Blocks-IPv6.csv` -> `GeoLite2-City-Blocks-IPv6`

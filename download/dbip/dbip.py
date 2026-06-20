import gzip
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone

import boto3  # type: ignore[import-not-found]
import requests


# Runs on the 1st day of every month at 12:00 UTC.
CRON_SCHEDULE_UTC = "0 12 1 * *"
EVENTBRIDGE_CRON_SCHEDULE_UTC = "cron(0 12 1 * ? *)"

DEFAULT_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; geo-dbip-downloader/1.0; +https://github.com/jblukach/geo)",
    "Accept": "text/csv,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

STREAM_CHUNK_SIZE = 8 * 1024 * 1024


def _build_datasets(now_utc: datetime) -> list[dict[str, str]]:
    year_month = now_utc.strftime("%Y-%m")

    return [
        {
            "name": "dbip-city-lite",
            "url": f"https://download.db-ip.com/free/dbip-city-lite-{year_month}.csv.gz",
            "compressed_file": f"dbip-city-lite-{year_month}.csv.gz",
            "extracted_file": f"dbip-city-lite-{year_month}.csv",
            "s3_key": "dbip-city-lite.csv",
        },
        {
            "name": "dbip-asn-lite",
            "url": f"https://download.db-ip.com/free/dbip-asn-lite-{year_month}.csv.gz",
            "compressed_file": f"dbip-asn-lite-{year_month}.csv.gz",
            "extracted_file": f"dbip-asn-lite-{year_month}.csv",
            "s3_key": "dbip-asn-lite.csv",
        },
    ]


def _download_file(url: str, output_path: str) -> None:
    with requests.get(
        url,
        headers=DEFAULT_REQUEST_HEADERS,
        timeout=300,
        stream=True,
    ) as response:
        response.raise_for_status()
        with open(output_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                if chunk:
                    output.write(chunk)


def _decompress_gzip(compressed_path: str, extracted_path: str) -> None:
    with gzip.open(compressed_path, "rb") as source:
        with open(extracted_path, "wb") as target:
            shutil.copyfileobj(source, target, STREAM_CHUNK_SIZE)


def handler(event, context):
    del event, context

    download_bucket = os.environ["DOWNLOAD_BUCKET_NAME"]
    s3_client = boto3.client("s3")

    now_utc = datetime.now(timezone.utc)
    datasets = _build_datasets(now_utc)

    result = {}

    with tempfile.TemporaryDirectory() as temp_dir:
        for dataset in datasets:
            compressed_path = os.path.join(temp_dir, dataset["compressed_file"])
            extracted_path = os.path.join(temp_dir, dataset["extracted_file"])

            try:
                _download_file(dataset["url"], compressed_path)
                _decompress_gzip(compressed_path, extracted_path)

                # List extracted files before any upload.
                extracted_files = [os.path.basename(extracted_path)]
                print(f"{dataset['name']} extracted files: {json.dumps(extracted_files)}")

                s3_client.upload_file(extracted_path, download_bucket, dataset["s3_key"])
            except requests.HTTPError as exc:
                response = exc.response
                status_code = response.status_code if response is not None else 0
                reason = response.reason if response is not None else ""
                if status_code == 403:
                    raise RuntimeError(
                        f"Failed processing {dataset['name']} from {dataset['url']}: 403 Forbidden. "
                        "The request used standard browser-like headers, so this is likely source IP policy/rate limiting from the upstream provider. "
                        "Use a static egress IP (NAT + EIP) and request allowlisting with DB-IP, or proxy through an allowed network."
                    ) from exc
                raise RuntimeError(
                    f"Failed processing {dataset['name']} from {dataset['url']}: {status_code} {reason}"
                ) from exc
            except requests.RequestException as exc:
                raise RuntimeError(
                    f"Failed processing {dataset['name']} from {dataset['url']}: {exc}"
                ) from exc

            result[dataset["name"]] = {
                "url": dataset["url"],
                "extracted_files": extracted_files,
            }

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "run_at_utc": now_utc.isoformat(),
                "cron_utc": CRON_SCHEDULE_UTC,
                "eventbridge_cron_utc": EVENTBRIDGE_CRON_SCHEDULE_UTC,
                "datasets": result,
            }
        ),
    }

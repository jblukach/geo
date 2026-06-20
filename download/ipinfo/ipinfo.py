import gzip
import io
import json
import os
from datetime import datetime, timezone
from urllib import parse

import boto3  # type: ignore[import-not-found]
import requests


# Runs every day at 12:00 UTC.
CRON_SCHEDULE_UTC = "0 12 * * *"
EVENTBRIDGE_CRON_SCHEDULE_UTC = "cron(0 12 * * ? *)"

DEFAULT_REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; geo-ipinfo-downloader/1.0; +https://github.com/jblukach/geo)",
    "Accept": "text/csv,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

STREAM_CHUNK_SIZE = 8 * 1024 * 1024
BASE_URL = "https://ipinfo.io/data/ipinfo_lite.csv.gz"
DATASET_NAME = "ipinfo-lite"
EXTRACTED_FILE_NAME = "ipinfo-lite.csv"
S3_KEY = "ipinfo-lite.csv"


def _build_request_url(token: str) -> str:
    query = parse.urlencode({"_src": "frontend", "token": token})
    return f"{BASE_URL}?{query}"


def _download_content(url: str) -> bytes:
    content = bytearray()

    with requests.get(
        url,
        headers=DEFAULT_REQUEST_HEADERS,
        timeout=300,
        stream=True,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
            if chunk:
                content.extend(chunk)

    return bytes(content)


def _decompress_gzip_content(compressed_content: bytes) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(compressed_content), mode="rb") as source:
        return source.read()


def handler(event, context):
    del event, context

    download_bucket = os.environ["DOWNLOAD_BUCKET_NAME"]
    secret_name = os.environ["SECRET_NAME"]
    token_key = os.environ.get("IPINFO_SECRET_KEY", "IPINFO")

    secrets_client = boto3.client("secretsmanager")
    s3_client = boto3.client("s3")

    secret_value = secrets_client.get_secret_value(SecretId=secret_name)
    credentials = json.loads(secret_value["SecretString"])
    token = credentials.get(token_key)

    if not token:
        raise RuntimeError(
            f"Missing IPInfo token in secret '{secret_name}' key '{token_key}'"
        )

    now_utc = datetime.now(timezone.utc)
    request_url = _build_request_url(token)

    try:
        compressed_content = _download_content(request_url)
        extracted_content = _decompress_gzip_content(compressed_content)

        extracted_files = [EXTRACTED_FILE_NAME]
        print(f"{DATASET_NAME} extracted files: {json.dumps(extracted_files)}")

        s3_client.put_object(Bucket=download_bucket, Key=S3_KEY, Body=extracted_content)
    except requests.HTTPError as exc:
        response = exc.response
        status_code = response.status_code if response is not None else 0
        reason = response.reason if response is not None else ""
        raise RuntimeError(
            f"Failed processing {DATASET_NAME} from {BASE_URL}: {status_code} {reason}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed processing {DATASET_NAME} from {BASE_URL}: {type(exc).__name__}"
        ) from exc

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "run_at_utc": now_utc.isoformat(),
                "cron_utc": CRON_SCHEDULE_UTC,
                "eventbridge_cron_utc": EVENTBRIDGE_CRON_SCHEDULE_UTC,
                "dataset": {
                    "name": DATASET_NAME,
                    "url": BASE_URL,
                    "extracted_files": extracted_files,
                    "s3_key": S3_KEY,
                },
            }
        ),
    }

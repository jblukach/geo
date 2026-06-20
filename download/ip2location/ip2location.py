import io
import json
import os
import zipfile
from datetime import datetime, timezone
from urllib import parse

import boto3  # type: ignore[import-not-found]
import requests


# Runs on the 1st day of every month at 12:00 UTC.
CRON_SCHEDULE_UTC = "0 12 1 * *"
EVENTBRIDGE_CRON_SCHEDULE_UTC = "cron(0 12 1 * ? *)"

BASE_URL = "https://www.ip2location.com/download"
STREAM_CHUNK_SIZE = 8 * 1024 * 1024

DATASETS = [
    {"name": "db11litecsv", "code": "DB11LITECSV"},
    {"name": "dbasnlite", "code": "DBASNLITE"},
    {"name": "px12litecsv", "code": "PX12LITECSV"},
    {"name": "db11litecsvipv6", "code": "DB11LITECSVIPV6"},
    {"name": "dbasnliteipv6", "code": "DBASNLITEIPV6"},
    {"name": "px12litecsvipv6", "code": "PX12LITECSVIPV6"},
]


def _build_dataset(token: str, code: str, name: str) -> dict[str, str]:
    query = parse.urlencode({"token": token, "file": code})

    return {
        "name": name,
        "code": code,
        "request_url": f"{BASE_URL}/?{query}",
        "public_url": f"{BASE_URL}/",
    }


def _download_content(url: str) -> bytes:
    content = bytearray()

    with requests.get(url, timeout=300, stream=True) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
            if chunk:
                content.extend(chunk)

    return bytes(content)


def _extract_and_upload_csv_files(s3_client, bucket_name: str, archive_content: bytes) -> tuple[list[str], list[str]]:
    uploaded_csv_files: list[str] = []

    archive_stream = io.BytesIO(archive_content)
    if zipfile.is_zipfile(archive_stream):
        archive_stream.seek(0)
        with zipfile.ZipFile(archive_stream, "r") as zip_file:
            file_infos = sorted(
                [info for info in zip_file.infolist() if not info.is_dir()],
                key=lambda info: info.filename,
            )
            extracted_files = [info.filename for info in file_infos]

            for file_info in file_infos:
                file_name = os.path.basename(file_info.filename)
                if not file_name.lower().endswith(".csv"):
                    continue

                with zip_file.open(file_info, "r") as file_content:
                    s3_client.upload_fileobj(file_content, bucket_name, file_name)
                uploaded_csv_files.append(file_name)
    else:
        # Fallback if the upstream sends plain CSV content instead of a zip archive.
        fallback_csv = "download.csv"
        extracted_files = [fallback_csv]
        s3_client.put_object(Bucket=bucket_name, Key=fallback_csv, Body=archive_content)
        uploaded_csv_files.append(fallback_csv)

    uploaded_csv_files.sort()
    return extracted_files, uploaded_csv_files


def handler(event, context):
    del event, context

    secret_name = os.environ["SECRET_NAME"]
    download_bucket = os.environ["DOWNLOAD_BUCKET_NAME"]
    token_key = os.environ.get("IP2LOCATION_SECRET_KEY", "IP2LOCATION")

    secrets_client = boto3.client("secretsmanager")
    s3_client = boto3.client("s3")

    secret_value = secrets_client.get_secret_value(SecretId=secret_name)
    credentials = json.loads(secret_value["SecretString"])
    token = credentials.get(token_key)

    if not token:
        raise RuntimeError(
            f"Missing IP2Location token in secret '{secret_name}' key '{token_key}'"
        )

    now_utc = datetime.now(timezone.utc)
    result = {}

    for dataset in DATASETS:
        resolved = _build_dataset(token, dataset["code"], dataset["name"])

        try:
            archive_content = _download_content(resolved["request_url"])
            extracted_files, uploaded_csv_files = _extract_and_upload_csv_files(
                s3_client,
                download_bucket,
                archive_content,
            )
        except requests.HTTPError as exc:
            response = exc.response
            status_code = response.status_code if response is not None else 0
            reason = response.reason if response is not None else ""
            raise RuntimeError(
                f"Failed processing {resolved['name']} from {resolved['public_url']}: {status_code} {reason}"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed processing {resolved['name']} from {resolved['public_url']}: {type(exc).__name__}"
            ) from exc

        print(
            f"{dataset['name']} ({dataset['code']}) completed | "
            f"extracted_file_count={len(extracted_files)} "
            f"uploaded_csv_count={len(uploaded_csv_files)}"
        )

        result[resolved["name"]] = {
            "code": resolved["code"],
            "url": resolved["public_url"],
            "extracted_files": extracted_files,
            "uploaded_csv_files": uploaded_csv_files,
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

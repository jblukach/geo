import json
import os
import shutil
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
        "url": f"{BASE_URL}/?{query}",
        "archive_path": f"/tmp/{code}.zip",
        "extract_dir": f"/tmp/{code}",
    }


def _download_file(url: str, output_path: str) -> None:
    with requests.get(url, timeout=300, stream=True) as response:
        response.raise_for_status()
        with open(output_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                if chunk:
                    output.write(chunk)


def _extract_archive(archive_path: str, extract_dir: str) -> list[str]:
    if os.path.isdir(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir, exist_ok=True)

    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path, "r") as zip_file:
            zip_file.extractall(extract_dir)
    else:
        # Fallback if the upstream sends plain CSV content instead of a zip archive.
        fallback_csv = os.path.join(extract_dir, "download.csv")
        shutil.copyfile(archive_path, fallback_csv)

    extracted_files = []
    for root, _, files in os.walk(extract_dir):
        for file_name in files:
            full_path = os.path.join(root, file_name)
            extracted_files.append(os.path.relpath(full_path, extract_dir))

    extracted_files.sort()
    return extracted_files


def _upload_csv_files(s3_client, bucket_name: str, extract_dir: str, code: str) -> list[str]:
    uploaded = []

    for root, _, files in os.walk(extract_dir):
        for file_name in files:
            if not file_name.lower().endswith(".csv"):
                continue

            local_path = os.path.join(root, file_name)
            s3_key = file_name
            s3_client.upload_file(local_path, bucket_name, s3_key)
            uploaded.append(s3_key)

    uploaded.sort()
    return uploaded


def _cleanup_path(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)


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
        archive_path = resolved["archive_path"]
        extract_dir = resolved["extract_dir"]

        try:
            _download_file(resolved["url"], archive_path)
            extracted_files = _extract_archive(archive_path, extract_dir)
            uploaded_csv_files = _upload_csv_files(
                s3_client,
                download_bucket,
                extract_dir,
                resolved["code"],
            )
        except requests.HTTPError as exc:
            response = exc.response
            status_code = response.status_code if response is not None else 0
            reason = response.reason if response is not None else ""
            raise RuntimeError(
                f"Failed processing {resolved['name']} from {resolved['url']}: {status_code} {reason}"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed processing {resolved['name']} from {resolved['url']}: {exc}"
            ) from exc
        finally:
            _cleanup_path(archive_path)
            _cleanup_path(extract_dir)

        print(
            f"{resolved['name']} extracted files: {json.dumps(extracted_files)} | "
            f"uploaded_csv_files: {json.dumps(uploaded_csv_files)}"
        )

        result[resolved["name"]] = {
            "code": resolved["code"],
            "url": resolved["url"],
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

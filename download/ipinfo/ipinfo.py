import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from urllib import error, parse, request

import boto3  # type: ignore[import-not-found]


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


def _build_dataset(token: str) -> dict[str, str]:
	query = parse.urlencode({"_src": "frontend", "token": token})

	return {
		"name": "ipinfo-lite",
		"url": f"{BASE_URL}?{query}",
		"compressed_file": "/tmp/ipinfo-lite.csv.gz",
		"extracted_file": "/tmp/ipinfo-lite.csv",
		"s3_key": "ipinfo-lite.csv",
	}


def _download_file(url: str, output_path: str) -> None:
	request_obj = request.Request(url, headers=DEFAULT_REQUEST_HEADERS, method="GET")
	with request.urlopen(request_obj, timeout=300) as response:
		with open(output_path, "wb") as output:
			shutil.copyfileobj(response, output, STREAM_CHUNK_SIZE)


def _decompress_gzip(compressed_path: str, extracted_path: str) -> None:
	with gzip.open(compressed_path, "rb") as source:
		with open(extracted_path, "wb") as target:
			shutil.copyfileobj(source, target, STREAM_CHUNK_SIZE)


def _cleanup(path: str) -> None:
	if os.path.isfile(path):
		os.remove(path)


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
	dataset = _build_dataset(token)

	compressed_path = dataset["compressed_file"]
	extracted_path = dataset["extracted_file"]

	try:
		_download_file(dataset["url"], compressed_path)
		_decompress_gzip(compressed_path, extracted_path)

		extracted_files = [os.path.basename(extracted_path)]
		print(f"{dataset['name']} extracted files: {json.dumps(extracted_files)}")

		s3_client.upload_file(extracted_path, download_bucket, dataset["s3_key"])
	except error.HTTPError as exc:
		raise RuntimeError(
			f"Failed processing {dataset['name']} from {dataset['url']}: {exc.code} {exc.reason}"
		) from exc
	except error.URLError as exc:
		raise RuntimeError(
			f"Failed processing {dataset['name']} from {dataset['url']}: {exc}"
		) from exc
	finally:
		_cleanup(compressed_path)
		_cleanup(extracted_path)

	return {
		"statusCode": 200,
		"body": json.dumps(
			{
				"run_at_utc": now_utc.isoformat(),
				"cron_utc": CRON_SCHEDULE_UTC,
				"eventbridge_cron_utc": EVENTBRIDGE_CRON_SCHEDULE_UTC,
				"dataset": {
					"name": dataset["name"],
					"url": dataset["url"],
					"extracted_files": extracted_files,
					"s3_key": dataset["s3_key"],
				},
			}
		),
	}

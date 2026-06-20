import json
import os
import shutil
import tempfile
import zipfile

import boto3  # type: ignore[import-not-found]
import requests


DATASETS = [
    {
        "name": "GeoLite2-ASN-CSV",
        "url": "https://download.maxmind.com/geoip/databases/GeoLite2-ASN-CSV/download?suffix=zip",
        "parameter": "SSM_PARAMETER_ASN_CSV",
        "files": [
            "GeoLite2-ASN-Blocks-IPv4.csv",
            "GeoLite2-ASN-Blocks-IPv6.csv",
        ],
    },
    {
        "name": "GeoLite2-City-CSV",
        "url": "https://download.maxmind.com/geoip/databases/GeoLite2-City-CSV/download?suffix=zip",
        "parameter": "SSM_PARAMETER_CITY_CSV",
        "files": [
            "GeoLite2-City-Blocks-IPv4.csv",
            "GeoLite2-City-Blocks-IPv6.csv",
            "GeoLite2-City-Locations-en.csv",
        ],
    },
]


def _header_token(headers) -> str:
    etag = headers.get("ETag", "")
    last_modified = headers.get("Last-Modified", "")
    return f"{etag}|{last_modified}"


def _get_stored_token(ssm_client, parameter_name: str) -> str:
    try:
        value = ssm_client.get_parameter(Name=parameter_name, WithDecryption=False)
        return value["Parameter"]["Value"]
    except ssm_client.exceptions.ParameterNotFound:
        return ""


def _put_stored_token(ssm_client, parameter_name: str, token: str) -> None:
    ssm_client.put_parameter(
        Name=parameter_name,
        Value=token,
        Type="String",
        Overwrite=True,
    )


def _extract_and_list(zip_path: str, output_dir: str):
    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zip_file:
        zip_file.extractall(output_dir)

    extracted = []
    for root, _, files in os.walk(output_dir):
        for file_name in files:
            full_path = os.path.join(root, file_name)
            extracted.append(os.path.relpath(full_path, output_dir))

    extracted.sort()
    return extracted


def _find_extracted_file(output_dir: str, file_name: str) -> str:
    for root, _, files in os.walk(output_dir):
        if file_name in files:
            return os.path.join(root, file_name)
    raise FileNotFoundError(f"Missing expected file after extraction: {file_name}")


def _upload_files(s3_client, bucket_name: str, output_dir: str, file_names: list[str]):
    for file_name in file_names:
        local_path = _find_extracted_file(output_dir, file_name)
        s3_client.upload_file(local_path, bucket_name, file_name)


def _cleanup_path(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)


def handler(event, context):
    del event, context

    selected_datasets = [
        dataset
        for dataset in DATASETS
        if os.environ.get(dataset["parameter"])
    ]

    if not selected_datasets:
        return {
            "statusCode": 200,
            "body": json.dumps({}),
        }

    secrets_client = boto3.client("secretsmanager")
    s3_client = boto3.client("s3")
    ssm_client = boto3.client("ssm")

    secret_name = os.environ["SECRET_NAME"]
    download_bucket = os.environ["DOWNLOAD_BUCKET_NAME"]
    secret_value = secrets_client.get_secret_value(SecretId=secret_name)
    credentials = json.loads(secret_value["SecretString"])
    auth = (credentials["GEOLITE_API"], credentials["GEOLITE_KEY"])

    result = {}

    for dataset in selected_datasets:
        parameter_name = os.environ[dataset["parameter"]]
        stored_token = _get_stored_token(ssm_client, parameter_name)

        try:
            head_response = requests.head(
                dataset["url"],
                auth=auth,
                timeout=60,
                allow_redirects=True,
            )
            head_response.raise_for_status()
            current_token = _header_token(head_response.headers)
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed header check for {dataset['name']}: {exc}") from exc

        if current_token and current_token == stored_token:
            result[dataset["name"]] = []
            continue

        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, f"{dataset['name']}.zip")
            extract_dir = os.path.join(temp_dir, dataset["name"])

            try:
                with requests.get(
                    dataset["url"],
                    auth=auth,
                    timeout=300,
                    stream=True,
                ) as get_response:
                    get_response.raise_for_status()
                    with open(zip_path, "wb") as output:
                        for chunk in get_response.iter_content(chunk_size=8 * 1024 * 1024):
                            if chunk:
                                output.write(chunk)

                extracted_files = _extract_and_list(zip_path, extract_dir)
                _upload_files(s3_client, download_bucket, extract_dir, dataset["files"])
            except requests.RequestException as exc:
                raise RuntimeError(f"Failed download for {dataset['name']}: {exc}") from exc

        print(f"{dataset['name']} extracted files: {json.dumps(extracted_files)}")

        if current_token:
            _put_stored_token(ssm_client, parameter_name, current_token)

        result[dataset["name"]] = extracted_files

    return {
        "statusCode": 200,
        "body": json.dumps(result),
    }

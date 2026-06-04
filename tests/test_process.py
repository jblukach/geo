import csv
import os
import tempfile
import unittest
from json import loads as json_loads
from unittest import mock

from process import process


def _write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class BuildOutputsTests(unittest.TestCase):

    def test_uses_registered_country_geoname_when_geoname_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Locations-en.csv"),
                [
                    "geoname_id",
                    "continent_code",
                    "continent_name",
                    "country_iso_code",
                    "country_name",
                    "subdivision_1_name",
                    "city_name",
                    "time_zone",
                ],
                [
                    {
                        "geoname_id": "100",
                        "continent_code": "NA",
                        "continent_name": "North America",
                        "country_iso_code": "US",
                        "country_name": "United States",
                        "subdivision_1_name": "Ohio",
                        "city_name": "Columbus",
                        "time_zone": "America/New_York",
                    },
                    {
                        "geoname_id": "200",
                        "continent_code": "EU",
                        "continent_name": "Europe",
                        "country_iso_code": "DE",
                        "country_name": "Germany",
                        "subdivision_1_name": "Berlin",
                        "city_name": "Berlin",
                        "time_zone": "Europe/Berlin",
                    },
                ],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Blocks-IPv4.csv"),
                ["network", "geoname_id", "registered_country_geoname_id"],
                [
                    {
                        "network": "1.0.0.0/31",
                        "geoname_id": "",
                        "registered_country_geoname_id": "100",
                    }
                ],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-ASN-Blocks-IPv4.csv"),
                [
                    "network",
                    "autonomous_system_number",
                    "autonomous_system_organization",
                ],
                [
                    {
                        "network": "1.0.0.0/31",
                        "autonomous_system_number": "13335",
                        "autonomous_system_organization": "Cloudflare",
                    }
                ],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Blocks-IPv6.csv"),
                ["network", "geoname_id", "registered_country_geoname_id"],
                [
                    {
                        "network": "2001:db8::/127",
                        "geoname_id": "200",
                        "registered_country_geoname_id": "",
                    }
                ],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-ASN-Blocks-IPv6.csv"),
                [
                    "network",
                    "autonomous_system_number",
                    "autonomous_system_organization",
                ],
                [
                    {
                        "network": "2001:db8::/126",
                        "autonomous_system_number": "64510",
                        "autonomous_system_organization": "Example IPv6",
                    }
                ],
            )

            outputs = process.build_outputs_from_directory(directory)

        asn_ipv4_lines = outputs["GeoLite2-ASN-Blocks-IPv4.txt"].strip().splitlines()
        asn_ipv6_lines = outputs["GeoLite2-ASN-Blocks-IPv6.txt"].strip().splitlines()
        city_ipv4_lines = outputs["GeoLite2-City-Blocks-IPv4.txt"].strip().splitlines()
        city_ipv6_lines = outputs["GeoLite2-City-Blocks-IPv6.txt"].strip().splitlines()

        self.assertEqual(
            asn_ipv4_lines,
            [
                "16777216|16777217|1.0.0.0/31|13335|Cloudflare"
            ],
        )
        self.assertEqual(
            asn_ipv6_lines,
            [
                "42540766411282592856903984951653826560|42540766411282592856903984951653826563|2001:db8::/126|64510|Example IPv6",
            ],
        )
        self.assertEqual(
            city_ipv4_lines,
            [
                "16777216|16777217|1.0.0.0/31|NA|US|Ohio|Columbus"
            ],
        )
        self.assertEqual(
            city_ipv6_lines,
            [
                "42540766411282592856903984951653826560|42540766411282592856903984951653826561|2001:db8::/127|EU|DE||Berlin",
            ],
        )

    def test_build_output_artifacts_reports_complete_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Locations-en.csv"),
                [
                    "geoname_id",
                    "continent_code",
                    "continent_name",
                    "country_iso_code",
                    "country_name",
                    "subdivision_1_name",
                    "city_name",
                    "time_zone",
                ],
                [
                    {
                        "geoname_id": "100",
                        "continent_code": "NA",
                        "continent_name": "North America",
                        "country_iso_code": "US",
                        "country_name": "United States",
                        "subdivision_1_name": "Ohio",
                        "city_name": "Columbus",
                        "time_zone": "America/New_York",
                    },
                    {
                        "geoname_id": "200",
                        "continent_code": "EU",
                        "continent_name": "Europe",
                        "country_iso_code": "DE",
                        "country_name": "Germany",
                        "subdivision_1_name": "Berlin",
                        "city_name": "Berlin",
                        "time_zone": "Europe/Berlin",
                    },
                ],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Blocks-IPv4.csv"),
                ["network", "geoname_id", "registered_country_geoname_id"],
                [{"network": "1.0.0.0/31", "geoname_id": "100", "registered_country_geoname_id": ""}],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-ASN-Blocks-IPv4.csv"),
                ["network", "autonomous_system_number", "autonomous_system_organization"],
                [{"network": "1.0.0.0/31", "autonomous_system_number": "13335", "autonomous_system_organization": "Cloudflare"}],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Blocks-IPv6.csv"),
                ["network", "geoname_id", "registered_country_geoname_id"],
                [{"network": "2001:db8::/127", "geoname_id": "200", "registered_country_geoname_id": ""}],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-ASN-Blocks-IPv6.csv"),
                ["network", "autonomous_system_number", "autonomous_system_organization"],
                [{"network": "2001:db8::/127", "autonomous_system_number": "64510", "autonomous_system_organization": "Example IPv6"}],
            )

            artifacts = process.build_output_artifacts(directory)

        self.assertEqual(artifacts["GeoLite2-ASN-Blocks-IPv4.txt"]["summary"]["outputRows"], 1)
        self.assertEqual(artifacts["GeoLite2-ASN-Blocks-IPv6.txt"]["summary"]["outputRows"], 1)
        self.assertEqual(artifacts["GeoLite2-City-Blocks-IPv4.txt"]["summary"]["outputRows"], 1)
        self.assertEqual(artifacts["GeoLite2-City-Blocks-IPv6.txt"]["summary"]["outputRows"], 1)


class HandlerTests(unittest.TestCase):

    def setUp(self):
        process.reset_runtime_state()

    def test_handler_enqueues_source_jobs(self):
        queued_messages = []

        class FakeS3Client:

            def head_object(self, Bucket, Key):
                del Bucket
                return {
                    "ETag": f'"etag-{Key}"',
                    "LastModified": "2026-05-31T00:00:00+00:00",
                }

        class FakeSqsClient:

            def send_message(self, QueueUrl, MessageBody):
                queued_messages.append((QueueUrl, json_loads(MessageBody)))
                return {"MessageId": "id"}

        event = {
            "Records": [
                {
                    "body": '{"detail":{"bucket":{"name":"download-bucket"},"object":{"key":"GeoLite2-ASN-Blocks-IPv4.csv"}}}'
                }
            ]
        }

        with mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "PROCESS_QUEUE_URL": "https://example.com/queue",
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                side_effect=lambda service: FakeS3Client() if service == "s3" else FakeSqsClient(),
            ):
                response = process.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["skipped"])
        self.assertEqual(response["skipReason"], "queued_source_jobs")
        self.assertEqual(response["queuedSourceKeys"], ["GeoLite2-ASN-Blocks-IPv4.csv"])
        self.assertEqual(response["impactedSourceKeys"], ["GeoLite2-ASN-Blocks-IPv4.csv"])
        self.assertEqual(
            queued_messages,
            [
                (
                    "https://example.com/queue",
                    {
                        "sourceKey": "GeoLite2-ASN-Blocks-IPv4.csv",
                        "jobType": "source_build",
                        "sourceSignature": mock.ANY,
                    },
                ),
            ],
        )

    def test_handler_enqueues_ipv6_source_for_prefixed_key(self):
        queued_messages = []

        class FakeS3Client:

            def head_object(self, Bucket, Key):
                del Bucket
                return {
                    "ETag": f'"etag-{Key}"',
                    "LastModified": "2026-05-31T00:00:00+00:00",
                }

        class FakeSqsClient:

            def send_message(self, QueueUrl, MessageBody):
                queued_messages.append((QueueUrl, json_loads(MessageBody)))
                return {"MessageId": "id"}

        event = {
            "Records": [
                {
                    "body": '{"detail":{"bucket":{"name":"download-bucket"},"object":{"key":"incoming/maxmind/GeoLite2-ASN-Blocks-IPv6.csv"}}}'
                }
            ]
        }

        with mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "PROCESS_QUEUE_URL": "https://example.com/queue",
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                side_effect=lambda service: FakeS3Client() if service == "s3" else FakeSqsClient(),
            ):
                response = process.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["skipped"])
        self.assertEqual(response["skipReason"], "queued_source_jobs")
        self.assertEqual(response["queuedSourceKeys"], ["GeoLite2-ASN-Blocks-IPv6.csv"])
        self.assertEqual(response["impactedSourceKeys"], ["GeoLite2-ASN-Blocks-IPv6.csv"])
        self.assertEqual(
            queued_messages,
            [
                (
                    "https://example.com/queue",
                    {
                        "sourceKey": "GeoLite2-ASN-Blocks-IPv6.csv",
                        "jobType": "source_build",
                        "sourceSignature": mock.ANY,
                    },
                ),
            ],
        )

    def test_handler_worker_job_uploads_single_source_output(self):
        source_files = {
            "GeoLite2-City-Locations-en.csv": "geoname_id,continent_name,country_name,subdivision_1_name,city_name,time_zone\n1,North America,United States,Ohio,Columbus,America/New_York\n",
            "GeoLite2-City-Blocks-IPv4.csv": "network,geoname_id,registered_country_geoname_id\n1.0.0.0/31,1,\n",
            "GeoLite2-ASN-Blocks-IPv4.csv": "network,autonomous_system_number,autonomous_system_organization\n1.0.0.0/31,13335,Cloudflare\n",
            "GeoLite2-City-Blocks-IPv6.csv": "network,geoname_id,registered_country_geoname_id\n2001:db8::/127,1,\n",
            "GeoLite2-ASN-Blocks-IPv6.csv": "network,autonomous_system_number,autonomous_system_organization\n2001:db8::/127,64510,Example IPv6\n",
        }
        uploaded = {}
        uploaded_parts = []

        class FakeS3Client:

            def __init__(self):
                self.last_download = None

            def download_file(self, bucket, key, filename):
                self.last_download = (bucket, key)
                with open(filename, "w", encoding="utf-8") as handle:
                    handle.write(source_files[key])

            def create_multipart_upload(self, Bucket, Key, ContentType):
                del ContentType
                del Bucket, Key
                uploaded_parts.clear()
                return {"UploadId": "upload-id"}

            def upload_part(self, Bucket, Key, PartNumber, UploadId, Body):
                del Bucket, Key, PartNumber, UploadId
                Body.seek(0)
                uploaded_parts.append(Body.read().decode("utf-8"))
                return {"ETag": '"etag"'}

            def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
                del UploadId, MultipartUpload
                uploaded[(Bucket, Key)] = "".join(uploaded_parts)

            def abort_multipart_upload(self, Bucket, Key, UploadId):
                del Bucket, Key, UploadId
                raise AssertionError("abort_multipart_upload should not be called")

        event = {
            "Records": [
                {
                    "body": '{"jobType":"source_build","sourceKey":"GeoLite2-ASN-Blocks-IPv4.csv"}'
                }
            ]
        }

        with mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "PROCESS_QUEUE_URL": "https://example.com/queue",
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                return_value=FakeS3Client(),
            ):
                response = process.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["skipReason"], "")
        self.assertEqual(response["mode"], "worker")
        self.assertEqual(
            sorted(uploaded.keys()),
            [("processed-bucket", "GeoLite2-ASN-Blocks-IPv4.txt")],
        )
        self.assertIn("16777216|16777217|1.0.0.0/31|13335|Cloudflare", uploaded[("processed-bucket", "GeoLite2-ASN-Blocks-IPv4.txt")])

    def test_handler_skips_rebuild_until_all_source_files_exist(self):
        uploaded = {}

        class MissingObjectError(Exception):

            def __init__(self, key):
                super().__init__(key)
                self.response = {"Error": {"Code": "404", "Key": key}}

        class FakeS3Client:

            def head_object(self, Bucket, Key):
                del Bucket
                if Key == "GeoLite2-City-Locations-en.csv":
                    raise MissingObjectError(Key)
                return {"ResponseMetadata": {"HTTPStatusCode": 200}}

            def download_file(self, bucket, key, filename):
                del bucket, key, filename
                raise AssertionError("download_file should not be called when sources are missing")

            def put_object(self, Bucket, Key, Body, ContentType):
                del Bucket, Key, Body, ContentType
                uploaded["called"] = True

        event = {
            "Records": [
                {
                    "body": '{"detail":{"bucket":{"name":"download-bucket"},"object":{"key":"GeoLite2-City-Blocks-IPv4.csv"}}}'
                }
            ]
        }

        with mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "PROCESS_QUEUE_URL": "https://example.com/queue",
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                return_value=FakeS3Client(),
            ):
                response = process.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["skipped"])
        self.assertEqual(response["skipReason"], "missing_source_files")
        self.assertEqual(response["outputs"], [])
        self.assertEqual(
            response["missingSourceFiles"],
            ["GeoLite2-City-Locations-en.csv"],
        )
        self.assertEqual(uploaded, {})

    def test_handler_skips_when_no_relevant_records(self):
        class FakeS3Client:

            def head_object(self, Bucket, Key):
                del Bucket, Key
                raise AssertionError("head_object should not be called")

        event = {
            "Records": [
                {
                    "body": '{"detail":{"bucket":{"name":"download-bucket"},"object":{"key":"some-other-file.csv"}}}'
                }
            ]
        }

        with mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "PROCESS_QUEUE_URL": "https://example.com/queue",
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                return_value=FakeS3Client(),
            ):
                response = process.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["skipped"])
        self.assertEqual(response["skipReason"], "no_relevant_records")
        self.assertEqual(response["outputs"], [])

    def test_handler_skips_when_source_signature_unchanged(self):
        source_files = {
            "GeoLite2-City-Locations-en.csv": "geoname_id,continent_name,country_name,subdivision_1_name,city_name,time_zone\n1,North America,United States,Ohio,Columbus,America/New_York\n",
            "GeoLite2-City-Blocks-IPv4.csv": "network,geoname_id,registered_country_geoname_id\n1.0.0.0/31,1,\n",
            "GeoLite2-ASN-Blocks-IPv4.csv": "network,autonomous_system_number,autonomous_system_organization\n1.0.0.0/31,13335,Cloudflare\n",
            "GeoLite2-City-Blocks-IPv6.csv": "network,geoname_id,registered_country_geoname_id\n2001:db8::/127,1,\n",
            "GeoLite2-ASN-Blocks-IPv6.csv": "network,autonomous_system_number,autonomous_system_organization\n2001:db8::/127,64510,Example IPv6\n",
        }
        uploaded = {}
        queued_messages = []

        class FakeS3Client:

            def head_object(self, Bucket, Key):
                del Bucket
                return {
                    "ETag": f'"etag-{Key}"',
                    "LastModified": "2026-05-31T00:00:00+00:00",
                }

            def download_file(self, bucket, key, filename):
                del bucket
                with open(filename, "w", encoding="utf-8") as handle:
                    handle.write(source_files[key])

            def put_object(self, Bucket, Key, Body, ContentType):
                del ContentType
                uploaded[(Bucket, Key)] = Body.decode("utf-8")

        class FakeSqsClient:

            def send_message(self, QueueUrl, MessageBody):
                queued_messages.append((QueueUrl, json_loads(MessageBody)))
                return {"MessageId": "id"}

        event = {
            "Records": [
                {
                    "body": '{"detail":{"bucket":{"name":"download-bucket"},"object":{"key":"GeoLite2-ASN-Blocks-IPv4.csv"}}}'
                }
            ]
        }

        with mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "PROCESS_QUEUE_URL": "https://example.com/queue",
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                side_effect=lambda service: FakeS3Client() if service == "s3" else FakeSqsClient(),
            ):
                first_response = process.handler(event, None)
                second_response = process.handler(event, None)

        self.assertTrue(first_response["skipped"])
        self.assertEqual(first_response["skipReason"], "queued_source_jobs")
        self.assertEqual(first_response["queuedSourceKeys"], ["GeoLite2-ASN-Blocks-IPv4.csv"])
        self.assertTrue(second_response["skipped"])
        self.assertEqual(second_response["skipReason"], "source_unchanged")
        self.assertEqual(
            sorted(uploaded.keys()),
            [],
        )
        self.assertEqual(len(queued_messages), 1)

if __name__ == "__main__":
    unittest.main()
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


class CombineIntervalsTests(unittest.TestCase):

    def test_splits_city_rows_and_adds_asn_only_gaps(self):
        city_intervals = [
            {
                "start": 0,
                "end": 7,
                "continent": "North America",
                "country": "United States",
                "subdivision": "Ohio",
                "city": "Columbus",
                "timezone": "America/New_York",
            }
        ]
        asn_intervals = [
            {
                "start": 0,
                "end": 3,
                "asn": "64500",
                "organization": "Example A",
            },
            {
                "start": 8,
                "end": 15,
                "asn": "64501",
                "organization": "Example B",
            },
        ]

        rows = process.combine_intervals(city_intervals, asn_intervals, 4)

        self.assertEqual(
            rows,
            [
                {
                    "startip": "0",
                    "endip": "3",
                    "network": "0.0.0.0/30",
                    "asn": "64500",
                    "organization": "Example A",
                    "continent": "North America",
                    "country": "United States",
                    "subdivision": "Ohio",
                    "city": "Columbus",
                    "timezone": "America/New_York",
                },
                {
                    "startip": "4",
                    "endip": "7",
                    "network": "0.0.0.4/30",
                    "asn": "",
                    "organization": "",
                    "continent": "North America",
                    "country": "United States",
                    "subdivision": "Ohio",
                    "city": "Columbus",
                    "timezone": "America/New_York",
                },
                {
                    "startip": "8",
                    "endip": "15",
                    "network": "0.0.0.8/29",
                    "asn": "64501",
                    "organization": "Example B",
                    "continent": "",
                    "country": "",
                    "subdivision": "",
                    "city": "",
                    "timezone": "",
                },
            ],
        )


class BuildOutputsTests(unittest.TestCase):

    def test_uses_registered_country_geoname_when_geoname_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Locations-en.csv"),
                [
                    "geoname_id",
                    "continent_name",
                    "country_name",
                    "subdivision_1_name",
                    "city_name",
                    "time_zone",
                ],
                [
                    {
                        "geoname_id": "100",
                        "continent_name": "North America",
                        "country_name": "United States",
                        "subdivision_1_name": "Ohio",
                        "city_name": "Columbus",
                        "time_zone": "America/New_York",
                    },
                    {
                        "geoname_id": "200",
                        "continent_name": "Europe",
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

        ipv4_lines = outputs["GeoLite2-IPv4.txt"].strip().splitlines()
        ipv6_lines = outputs["GeoLite2-IPv6.txt"].strip().splitlines()

        self.assertEqual(
            ipv4_lines,
            [
                "16777216|16777217|1.0.0.0/31|13335|Cloudflare|North America|United States|Ohio|Columbus|America/New_York"
            ],
        )
        self.assertEqual(
            ipv6_lines,
            [
                "42540766411282592856903984951653826560|42540766411282592856903984951653826561|2001:db8::/127|64510|Example IPv6|Europe|Germany|Berlin|Berlin|Europe/Berlin",
                "42540766411282592856903984951653826562|42540766411282592856903984951653826563|2001:db8::2/127|64510|Example IPv6|||||",
            ],
        )

    def test_build_output_artifacts_reports_complete_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            _write_csv(
                os.path.join(directory, "GeoLite2-City-Locations-en.csv"),
                [
                    "geoname_id",
                    "continent_name",
                    "country_name",
                    "subdivision_1_name",
                    "city_name",
                    "time_zone",
                ],
                [
                    {
                        "geoname_id": "100",
                        "continent_name": "North America",
                        "country_name": "United States",
                        "subdivision_1_name": "Ohio",
                        "city_name": "Columbus",
                        "time_zone": "America/New_York",
                    },
                    {
                        "geoname_id": "200",
                        "continent_name": "Europe",
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

        ipv4_summary = artifacts["GeoLite2-IPv4.txt"]["summary"]
        ipv6_summary = artifacts["GeoLite2-IPv6.txt"]["summary"]

        self.assertTrue(ipv4_summary["cityCoverageComplete"])
        self.assertTrue(ipv4_summary["asnCoverageComplete"])
        self.assertTrue(ipv4_summary["unionCoverageComplete"])
        self.assertEqual(ipv4_summary["outputRows"], 1)
        self.assertEqual(ipv4_summary["outputAddresses"], 2)

        self.assertTrue(ipv6_summary["cityCoverageComplete"])
        self.assertTrue(ipv6_summary["asnCoverageComplete"])
        self.assertTrue(ipv6_summary["unionCoverageComplete"])
        self.assertEqual(ipv6_summary["outputRows"], 1)
        self.assertEqual(ipv6_summary["outputAddresses"], 2)


class HandlerTests(unittest.TestCase):

    def setUp(self):
        process.reset_runtime_state()

    def test_handler_enqueues_family_jobs(self):
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
        self.assertEqual(response["skipReason"], "queued_family_jobs")
        self.assertEqual(response["queuedFamilies"], [4])
        self.assertEqual(response["impactedFamilies"], [4])
        self.assertEqual(
            queued_messages,
            [
                (
                    "https://example.com/queue",
                    {
                        "family": 4,
                        "jobType": "family_build",
                        "sourceSignature": mock.ANY,
                    },
                ),
            ],
        )

    def test_handler_worker_job_uploads_single_family_output(self):
        source_files = {
            "GeoLite2-City-Locations-en.csv": "geoname_id,continent_name,country_name,subdivision_1_name,city_name,time_zone\n1,North America,United States,Ohio,Columbus,America/New_York\n",
            "GeoLite2-City-Blocks-IPv4.csv": "network,geoname_id,registered_country_geoname_id\n1.0.0.0/31,1,\n",
            "GeoLite2-ASN-Blocks-IPv4.csv": "network,autonomous_system_number,autonomous_system_organization\n1.0.0.0/31,13335,Cloudflare\n",
            "GeoLite2-City-Blocks-IPv6.csv": "network,geoname_id,registered_country_geoname_id\n2001:db8::/127,1,\n",
            "GeoLite2-ASN-Blocks-IPv6.csv": "network,autonomous_system_number,autonomous_system_organization\n2001:db8::/127,64510,Example IPv6\n",
        }
        uploaded = {}

        class FakeS3Client:

            def __init__(self):
                self.last_download = None

            def download_file(self, bucket, key, filename):
                self.last_download = (bucket, key)
                with open(filename, "w", encoding="utf-8") as handle:
                    handle.write(source_files[key])

            def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
                del ExtraArgs
                with open(Filename, "r", encoding="utf-8") as handle:
                    uploaded[(Bucket, Key)] = handle.read()

        event = {
            "Records": [
                {
                    "body": '{"jobType":"family_build","family":4}'
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
            [("processed-bucket", "GeoLite2-IPv4.txt")],
        )
        self.assertIn("1.0.0.0/31|13335|Cloudflare", uploaded[("processed-bucket", "GeoLite2-IPv4.txt")])

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
        self.assertEqual(first_response["skipReason"], "queued_family_jobs")
        self.assertEqual(first_response["queuedFamilies"], [4])
        self.assertTrue(second_response["skipped"])
        self.assertEqual(second_response["skipReason"], "source_unchanged")
        self.assertEqual(
            sorted(uploaded.keys()),
            [],
        )
        self.assertEqual(len(queued_messages), 1)

if __name__ == "__main__":
    unittest.main()
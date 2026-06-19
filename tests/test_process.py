import csv
import json
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

    def test_momento_credential_provider_uses_from_string_for_legacy_token(self):
        calls = []

        class FakeCredentialProvider:

            @staticmethod
            def from_string(token):
                calls.append(("from_string", token))
                return "legacy-provider"

            @staticmethod
            def from_api_key(token):
                calls.append(("from_api_key", token))
                return "v2-provider"

        provider = process._momento_credential_provider(FakeCredentialProvider, "legacy-token")

        self.assertEqual(provider, "legacy-provider")
        self.assertEqual(calls, [("from_string", "legacy-token")])

    def test_momento_credential_provider_falls_back_to_api_key_for_v2_token(self):
        calls = []

        class FakeCredentialProvider:

            @staticmethod
            def from_string(token):
                calls.append(("from_string", token))
                raise ValueError("Unexpectedly received a v2 API key")

            @staticmethod
            def from_api_key_v2(token, endpoint):
                calls.append(("from_api_key_v2", token, endpoint))
                return "v2-provider"

        provider = process._momento_credential_provider(
            FakeCredentialProvider,
            "momt_abc123",
            "https://cache.cell-1-us-east-1-1.prod.a.momentohq.com",
        )

        self.assertEqual(provider, "v2-provider")
        self.assertEqual(
            calls,
            [
                ("from_string", "momt_abc123"),
                ("from_api_key_v2", "momt_abc123", "cell-1-us-east-1-1.prod.a.momentohq.com"),
            ],
        )

    def test_momento_credential_provider_requires_endpoint_for_v2_api_keys(self):
        class FakeCredentialProvider:

            @staticmethod
            def from_string(token):
                del token
                raise ValueError("Unexpectedly received a v2 API key")

            @staticmethod
            def from_api_key_v2(token, endpoint):
                del token, endpoint
                return "v2-provider"

        with self.assertRaisesRegex(RuntimeError, "MOMENTO_ENDPOINT"):
            process._momento_credential_provider(FakeCredentialProvider, "momt_abc123", "")

    def test_momento_credential_provider_raises_unexpected_from_string_error(self):
        class FakeCredentialProvider:

            @staticmethod
            def from_string(token):
                del token
                raise ValueError("some other auth failure")

        with self.assertRaisesRegex(ValueError, "some other auth failure"):
            process._momento_credential_provider(FakeCredentialProvider, "legacy-token")

    def test_momento_cache_name_for_source_uses_source_mapping(self):
        with mock.patch.dict(
            os.environ,
            {
                "MOMENTO_CACHE_NAMES_BY_SOURCE": json.dumps(
                    {
                        "GeoLite2-ASN-Blocks-IPv4.csv": "GeoLite2-ASN-Blocks-IPv4",
                        "GeoLite2-ASN-Blocks-IPv6.csv": "GeoLite2-ASN-Blocks-IPv6",
                        "GeoLite2-City-Blocks-IPv4.csv": "GeoLite2-City-Blocks-IPv4",
                        "GeoLite2-City-Blocks-IPv6.csv": "GeoLite2-City-Blocks-IPv6",
                    },
                    sort_keys=True,
                ),
            },
            clear=False,
        ):
            self.assertEqual(
                process._momento_cache_name_for_source("GeoLite2-ASN-Blocks-IPv4.csv"),
                "GeoLite2-ASN-Blocks-IPv4",
            )
            self.assertEqual(
                process._momento_cache_name_for_source("GeoLite2-City-Blocks-IPv6.csv"),
                "GeoLite2-City-Blocks-IPv6",
            )

    def test_momento_lookup_fields_for_ip_match_network_scores(self):
        ipv4_lookup = process.momento_lookup_fields_for_ip("1.0.0.1")
        ipv6_lookup = process.momento_lookup_fields_for_ip("2001:db8::1")

        self.assertEqual(ipv4_lookup["ip_version"], "4")
        self.assertEqual(
            ipv4_lookup["momento_score"],
            process._momento_score_for_network("1.0.0.1/32"),
        )
        self.assertEqual(ipv4_lookup["prefix_len"], "32")
        self.assertEqual(ipv4_lookup["sort_key"], "032")
        self.assertTrue(ipv4_lookup["shard"].startswith("v4:"))
        self.assertTrue(ipv4_lookup["asn_momento_set"].startswith("geo:asn:v4:"))
        self.assertTrue(ipv4_lookup["city_momento_set"].startswith("geo:city:v4:"))
        self.assertEqual(len(ipv4_lookup["ip_hex"]), 32)

        self.assertEqual(ipv6_lookup["ip_version"], "6")
        self.assertEqual(
            ipv6_lookup["momento_score"],
            process._momento_score_for_network("2001:db8::1/128"),
        )
        self.assertEqual(ipv6_lookup["prefix_len"], "128")
        self.assertEqual(ipv6_lookup["sort_key"], "128")
        self.assertTrue(ipv6_lookup["shard"].startswith("v6:"))
        self.assertTrue(ipv6_lookup["asn_momento_set"].startswith("geo:asn:v6:"))
        self.assertTrue(ipv6_lookup["city_momento_set"].startswith("geo:city:v6:"))
        self.assertEqual(len(ipv6_lookup["ip_hex"]), 32)

    def test_momento_lookup_score_for_ip_returns_score(self):
        self.assertEqual(
            process.momento_lookup_score_for_ip("1.0.0.1"),
            process.momento_lookup_fields_for_ip("1.0.0.1")["momento_score"],
        )

    def test_momento_lookup_fields_include_release_namespace_when_configured(self):
        with mock.patch.dict(
            os.environ,
            {
                "MOMENTO_SET_PREFIX": "geo",
                "MOMENTO_RELEASE": "r20260619",
            },
            clear=False,
        ):
            lookup = process.momento_lookup_fields_for_ip("1.0.0.1")

        self.assertTrue(lookup["asn_momento_set"].startswith("geo:r20260619:asn:v4:"))
        self.assertTrue(lookup["city_momento_set"].startswith("geo:r20260619:city:v4:"))

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
                "|".join(
                    [
                        process._momento_score_for_network("1.0.0.0/31"),
                        "4",
                        process._momento_shard_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False)),
                        process._momento_set_for_dataset_and_parsed_network("asn", process.ipaddress.ip_network("1.0.0.0/31", strict=False)),
                        "031",
                        "31",
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False))[0],
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False))[1],
                        "1.0.0.0/31",
                        "13335",
                        "Cloudflare",
                    ]
                )
            ],
        )
        self.assertEqual(
            asn_ipv6_lines,
            [
                "|".join(
                    [
                        process._momento_score_for_network("2001:db8::/126"),
                        "6",
                        process._momento_shard_for_parsed_network(process.ipaddress.ip_network("2001:db8::/126", strict=False)),
                        process._momento_set_for_dataset_and_parsed_network("asn", process.ipaddress.ip_network("2001:db8::/126", strict=False)),
                        "126",
                        "126",
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("2001:db8::/126", strict=False))[0],
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("2001:db8::/126", strict=False))[1],
                        "2001:db8::/126",
                        "64510",
                        "Example IPv6",
                    ]
                ),
            ],
        )
        self.assertEqual(
            city_ipv4_lines,
            [
                "|".join(
                    [
                        process._momento_score_for_network("1.0.0.0/31"),
                        "4",
                        process._momento_shard_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False)),
                        process._momento_set_for_dataset_and_parsed_network("city", process.ipaddress.ip_network("1.0.0.0/31", strict=False)),
                        "031",
                        "31",
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False))[0],
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False))[1],
                        "1.0.0.0/31",
                        "US",
                        "United States",
                        "Ohio",
                        "Columbus",
                    ]
                )
            ],
        )
        self.assertEqual(
            city_ipv6_lines,
            [
                "|".join(
                    [
                        process._momento_score_for_network("2001:db8::/127"),
                        "6",
                        process._momento_shard_for_parsed_network(process.ipaddress.ip_network("2001:db8::/127", strict=False)),
                        process._momento_set_for_dataset_and_parsed_network("city", process.ipaddress.ip_network("2001:db8::/127", strict=False)),
                        "127",
                        "127",
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("2001:db8::/127", strict=False))[0],
                        process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("2001:db8::/127", strict=False))[1],
                        "2001:db8::/127",
                        "DE",
                        "Germany",
                        "",
                        "Berlin",
                    ]
                ),
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

    def test_build_outputs_uses_release_namespace_when_configured(self):
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
                [{"network": "2001:db8::/127", "geoname_id": "100", "registered_country_geoname_id": ""}],
            )
            _write_csv(
                os.path.join(directory, "GeoLite2-ASN-Blocks-IPv6.csv"),
                ["network", "autonomous_system_number", "autonomous_system_organization"],
                [{"network": "2001:db8::/127", "autonomous_system_number": "64510", "autonomous_system_organization": "Example IPv6"}],
            )

            with mock.patch.dict(
                os.environ,
                {
                    "MOMENTO_SET_PREFIX": "geo",
                    "MOMENTO_RELEASE": "r20260619",
                },
                clear=False,
            ):
                outputs = process.build_outputs_from_directory(directory)

        asn_ipv4_first_line = outputs["GeoLite2-ASN-Blocks-IPv4.txt"].strip().splitlines()[0]
        self.assertIn("geo:r20260619:asn:v4:", asn_ipv4_first_line)


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
                "MOMENTO_CACHE_NAMES_BY_SOURCE": json.dumps(
                    {
                        "GeoLite2-ASN-Blocks-IPv4.csv": "GeoLite2-ASN-Blocks-IPv4",
                        "GeoLite2-ASN-Blocks-IPv6.csv": "GeoLite2-ASN-Blocks-IPv6",
                        "GeoLite2-City-Blocks-IPv4.csv": "GeoLite2-City-Blocks-IPv4",
                        "GeoLite2-City-Blocks-IPv6.csv": "GeoLite2-City-Blocks-IPv6",
                    },
                    sort_keys=True,
                ),
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
                "MOMENTO_CACHE_NAMES_BY_SOURCE": json.dumps(
                    {
                        "GeoLite2-ASN-Blocks-IPv4.csv": "GeoLite2-ASN-Blocks-IPv4",
                        "GeoLite2-ASN-Blocks-IPv6.csv": "GeoLite2-ASN-Blocks-IPv6",
                        "GeoLite2-City-Blocks-IPv4.csv": "GeoLite2-City-Blocks-IPv4",
                        "GeoLite2-City-Blocks-IPv6.csv": "GeoLite2-City-Blocks-IPv6",
                    },
                    sort_keys=True,
                ),
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
                "MOMENTO_CACHE_NAMES_BY_SOURCE": json.dumps(
                    {
                        "GeoLite2-ASN-Blocks-IPv4.csv": "GeoLite2-ASN-Blocks-IPv4",
                        "GeoLite2-ASN-Blocks-IPv6.csv": "GeoLite2-ASN-Blocks-IPv6",
                        "GeoLite2-City-Blocks-IPv4.csv": "GeoLite2-City-Blocks-IPv4",
                        "GeoLite2-City-Blocks-IPv6.csv": "GeoLite2-City-Blocks-IPv6",
                    },
                    sort_keys=True,
                ),
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
        self.assertIn(
            "|".join(
                [
                    process._momento_score_for_network("1.0.0.0/31"),
                    "4",
                    process._momento_shard_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False)),
                    process._momento_set_for_dataset_and_parsed_network("asn", process.ipaddress.ip_network("1.0.0.0/31", strict=False)),
                    "031",
                    "31",
                    process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False))[0],
                    process._momento_range_hex_for_parsed_network(process.ipaddress.ip_network("1.0.0.0/31", strict=False))[1],
                    "1.0.0.0/31",
                    "13335",
                    "Cloudflare",
                ]
            ),
            uploaded[("processed-bucket", "GeoLite2-ASN-Blocks-IPv4.txt")],
        )

    def test_handler_worker_job_loads_rows_into_momento_when_enabled(self):
        source_files = {
            "GeoLite2-ASN-Blocks-IPv4.csv": "network,autonomous_system_number,autonomous_system_organization\n1.0.0.0/31,13335,Cloudflare\n",
        }
        uploaded = {}

        class FakeS3Client:

            def create_multipart_upload(self, Bucket, Key, ContentType):
                del Bucket, Key, ContentType
                return {"UploadId": "upload-id"}

            def download_file(self, bucket, key, filename):
                del bucket
                with open(filename, "w", encoding="utf-8") as handle:
                    handle.write(source_files[key])

            def upload_part(self, Bucket, Key, PartNumber, UploadId, Body):
                del Bucket, PartNumber, UploadId
                Body.seek(0)
                uploaded[Key] = Body.read().decode("utf-8")
                return {"ETag": '"etag"'}

            def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
                del Bucket, Key, UploadId, MultipartUpload

            def abort_multipart_upload(self, Bucket, Key, UploadId):
                del Bucket, Key, UploadId
                raise AssertionError("abort_multipart_upload should not be called")

        momento_calls = []

        class FakeMomentoClient:

            def sorted_set_put_elements(self, cache_name, set_name, elements):
                momento_calls.append(
                    {
                        "cache_name": cache_name,
                        "set_name": set_name,
                        "elements": dict(elements),
                    }
                )

                class Success:
                    pass

                return Success()

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
                "MOMENTO_CACHE_NAMES_BY_SOURCE": json.dumps(
                    {
                        "GeoLite2-ASN-Blocks-IPv4.csv": "GeoLite2-ASN-Blocks-IPv4",
                        "GeoLite2-ASN-Blocks-IPv6.csv": "GeoLite2-ASN-Blocks-IPv6",
                        "GeoLite2-City-Blocks-IPv4.csv": "GeoLite2-City-Blocks-IPv4",
                        "GeoLite2-City-Blocks-IPv6.csv": "GeoLite2-City-Blocks-IPv6",
                    },
                    sort_keys=True,
                ),
            },
            clear=False,
        ):
            with mock.patch.object(
                process,
                "_boto3_client",
                return_value=FakeS3Client(),
            ):
                with mock.patch.object(
                    process,
                    "_momento_context",
                    return_value={
                        "cache_name": "geo",
                        "client": FakeMomentoClient(),
                        "loaded_rows": 0,
                        "set_names": set(),
                    },
                ):
                    response = process.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertTrue(response["momentoEnabled"])
        self.assertEqual(response["momentoLoadedRows"], 1)
        self.assertEqual(response["momentoSetCount"], 1)
        self.assertEqual(len(momento_calls), 1)
        self.assertEqual(momento_calls[0]["cache_name"], "geo")
        self.assertTrue(momento_calls[0]["set_name"].startswith("geo:asn:v4:"))
        self.assertEqual(len(momento_calls[0]["elements"]), 1)
        self.assertIn('"asn":"13335"', next(iter(momento_calls[0]["elements"])))

    def test_momento_bulk_write_retries_on_rate_limit(self):
        rows = [
            {
                "momento_set": "geo:asn:v4:0000",
                "momento_score": "1",
                "ip_version": "4",
                "prefix_len": "31",
                "range_start_hex": "00000000000000000000000000000000",
                "range_end_hex": "00000000000000000000000000000001",
                "network": "1.0.0.0/31",
                "asn": "13335",
                "organization": "Cloudflare",
            }
        ]
        writes = []

        def fake_put_sorted_set_elements(client, cache_name, set_name, elements):
            del client
            writes.append((cache_name, set_name, dict(elements)))
            if len(writes) == 1:
                raise RuntimeError("Momento sorted_set_put_elements failed: Operations rate limit exceeded")

        with mock.patch.object(process, "_momento_put_sorted_set_elements", side_effect=fake_put_sorted_set_elements):
            with mock.patch.object(process.time, "sleep") as sleep_mock:
                momento_context = {
                    "cache_name": "geo",
                    "client": object(),
                    "loaded_rows": 0,
                    "set_names": set(),
                }

                with mock.patch.dict(
                    os.environ,
                    {"MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS": "0"},
                    clear=False,
                ):
                    process._momento_add_output_rows(
                        momento_context,
                        "GeoLite2-ASN-Blocks-IPv4.txt",
                        "asn",
                        rows,
                    )

        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][0], "geo")
        self.assertEqual(writes[0][1], "geo:asn:v4:0000")
        sleep_mock.assert_called_once_with(1.0)
        self.assertEqual(momento_context["loaded_rows"], 1)
        self.assertEqual(momento_context["set_names"], {"geo:asn:v4:0000"})

    def test_momento_put_sorted_set_elements_wraps_raw_resource_exhausted_rpc(self):
        class ResourceExhaustedError(Exception):

            def code(self):
                class Status:
                    name = "RESOURCE_EXHAUSTED"

                return Status()

            def __str__(self):
                return "RESOURCE_EXHAUSTED: Operations rate limit exceeded"

        class FakeClient:

            def sorted_set_put_elements(self, cache_name, set_name, elements):
                del cache_name, set_name, elements
                raise ResourceExhaustedError()

        with self.assertRaisesRegex(RuntimeError, "RESOURCE_EXHAUSTED"):
            process._momento_put_sorted_set_elements(
                FakeClient(),
                "geo",
                "geo:asn:v4:0000",
                {"payload": 1.0},
            )

    def test_momento_bulk_write_chunks_by_request_size(self):
        rows = []
        for index in range(4):
            rows.append(
                {
                    "momento_set": "geo:asn:v4:0000",
                    "momento_score": str(index),
                    "ip_version": "4",
                    "prefix_len": "31",
                    "range_start_hex": "00000000000000000000000000000000",
                    "range_end_hex": "00000000000000000000000000000001",
                    "network": f"1.0.0.{index}/31",
                    "asn": "13335",
                    "organization": "Cloudflare" + ("x" * 400000 if index % 2 == 0 else ""),
                }
            )

        writes = []

        def fake_put_sorted_set_elements(client, cache_name, set_name, elements):
            del client
            writes.append((cache_name, set_name, dict(elements)))

        with mock.patch.object(process, "MOMENTO_SORTED_SET_REQUEST_SIZE_BYTES", 1000):
            with mock.patch.object(process, "_momento_put_sorted_set_elements", side_effect=fake_put_sorted_set_elements):
                with mock.patch.dict(
                    os.environ,
                    {"MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS": "0"},
                    clear=False,
                ):
                    momento_context = {
                        "cache_name": "geo",
                        "client": object(),
                        "loaded_rows": 0,
                        "set_names": set(),
                    }

                    process._momento_add_output_rows(
                        momento_context,
                        "GeoLite2-ASN-Blocks-IPv4.txt",
                        "asn",
                        rows,
                    )

        self.assertGreater(len(writes), 1)
        self.assertEqual(momento_context["loaded_rows"], 4)
        self.assertEqual(momento_context["set_names"], {"geo:asn:v4:0000"})

    def test_momento_request_slot_waits_between_calls(self):
        with mock.patch.dict(
            os.environ,
            {"MOMENTO_SORTED_SET_MIN_INTERVAL_SECONDS": "1.5"},
            clear=False,
        ):
            with mock.patch.object(process.time, "monotonic", side_effect=[10.2, 11.7]):
                with mock.patch.object(process.time, "sleep") as sleep_mock:
                    process.reset_runtime_state()
                    process._RUNTIME_STATE["last_momento_request_at"] = 10.0
                    process._momento_wait_for_request_slot()

                sleep_mock.assert_called_once()
                self.assertAlmostEqual(sleep_mock.call_args.args[0], 1.3, places=6)

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
import asyncio
import csv
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from process import process


def _write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class ValkeyLookupTests(unittest.TestCase):

    def test_valkey_lookup_member_uses_zrangebyscore_contract(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def zrangebyscore(self, set_name, min_score, max_score, start, num, desc, withscores):
                self.calls.append((set_name, min_score, max_score, start, num, desc, withscores))
                return ["16777217|asn|1.0.0.0/31|4|13335|Cloudflare"]

        client = FakeClient()
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                "VALKEY_ASN_V6_SET_NAME": "asn_v6_ranges",
                "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
                "VALKEY_CITY_V6_SET_NAME": "city_v6_ranges",
            },
            clear=False,
        ):
            member = asyncio.run(process.valkey_lookup_member(client, "1.0.0.1", "asn"))

        self.assertEqual(member, "16777217|asn|1.0.0.0/31|4|13335|Cloudflare")
        self.assertEqual(
            client.calls,
            [("asn_v4_ranges", 0, 16777217, 0, 1, True, False)],
        )

    def test_valkey_lookup_member_rejects_when_ip_above_end_bound(self):
        class FakeClient:
            async def zrangebyscore(self, *args, **kwargs):
                del args, kwargs
                return ["16777216|asn|1.0.0.0/31|4|13335|Cloudflare"]

        member = asyncio.run(process.valkey_lookup_member(FakeClient(), "1.0.0.1", "asn"))
        self.assertIsNone(member)


class ValkeyContextTests(unittest.TestCase):

    def test_run_valkey_coroutine_reuses_context_loop(self):
        loop = asyncio.new_event_loop()
        context = {"loop": loop}

        async def _loop_id():
            return id(asyncio.get_running_loop())

        try:
            first_loop_id = process._run_valkey_coroutine(context, _loop_id())
            second_loop_id = process._run_valkey_coroutine(context, _loop_id())
        finally:
            loop.close()

        self.assertEqual(first_loop_id, id(loop))
        self.assertEqual(second_loop_id, id(loop))

    def test_valkey_context_tls_uses_ssl_connection_class(self):
        captured_kwargs = {}

        class FakePool:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            async def aclose(self):
                return None

        class FakeRedisClient:
            def __init__(self, connection_pool):
                self.connection_pool = connection_pool

            async def aclose(self):
                return None

        fake_redis_module = types.ModuleType("redis.asyncio")
        fake_redis_module.ConnectionPool = FakePool
        fake_redis_module.Redis = FakeRedisClient
        fake_redis_module.SSLConnection = object
        fake_redis_package = types.ModuleType("redis")
        fake_redis_package.asyncio = fake_redis_module

        with mock.patch.dict(
            sys.modules,
            {
                "redis": fake_redis_package,
                "redis.asyncio": fake_redis_module,
            },
            clear=False,
        ):
            with mock.patch.dict(
                os.environ,
                {
                    "VALKEY_ENDPOINT": "cache.example",
                    "VALKEY_TLS": "true",
                },
                clear=False,
            ):
                context = process._valkey_context()

        try:
            self.assertIsNotNone(context)
            self.assertEqual(captured_kwargs["connection_class"], fake_redis_module.SSLConnection)
            self.assertNotIn("ssl", captured_kwargs)
            self.assertNotIn("password", captured_kwargs)
        finally:
            process._close_valkey_context(context)

    def test_valkey_context_non_tls_does_not_set_connection_class(self):
        captured_kwargs = {}

        class FakePool:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            async def aclose(self):
                return None

        class FakeRedisClient:
            def __init__(self, connection_pool):
                self.connection_pool = connection_pool

            async def aclose(self):
                return None

        fake_redis_module = types.ModuleType("redis.asyncio")
        fake_redis_module.ConnectionPool = FakePool
        fake_redis_module.Redis = FakeRedisClient
        fake_redis_module.SSLConnection = object
        fake_redis_package = types.ModuleType("redis")
        fake_redis_package.asyncio = fake_redis_module

        with mock.patch.dict(
            sys.modules,
            {
                "redis": fake_redis_package,
                "redis.asyncio": fake_redis_module,
            },
            clear=False,
        ):
            with mock.patch.dict(
                os.environ,
                {
                    "VALKEY_ENDPOINT": "cache.example",
                    "VALKEY_TLS": "false",
                },
                clear=False,
            ):
                context = process._valkey_context()

        try:
            self.assertIsNotNone(context)
            self.assertNotIn("connection_class", captured_kwargs)
            self.assertNotIn("ssl", captured_kwargs)
        finally:
            process._close_valkey_context(context)


class ValkeyMetadataTests(unittest.TestCase):

    def test_valkey_set_last_updated_writes_dataset_key(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def set(self, key, value):
                self.calls.append((key, value))
                return True

        fake_client = FakeClient()
        with mock.patch.object(process, "_utc_now_iso8601", return_value="2026-06-19T12:00:00Z"):
            with mock.patch.dict(
                os.environ,
                {
                    "VALKEY_LAST_UPDATED_ASN_KEY": "geo:last_updated:asn",
                },
                clear=False,
            ):
                asyncio.run(
                    process._valkey_set_last_updated(
                        {"client": fake_client},
                        "asn",
                    )
                )

        self.assertEqual(fake_client.calls, [("geo:last_updated:asn", "2026-06-19T12:00:00Z")])


class OutputRowTests(unittest.TestCase):

    def test_iter_asn_output_rows_uses_dataset_specific_set_names(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path = os.path.join(directory, "GeoLite2-ASN-Blocks-IPv4.csv")
            _write_csv(
                csv_path,
                ["network", "autonomous_system_number", "autonomous_system_organization"],
                [
                    {
                        "network": "1.0.0.0/31",
                        "autonomous_system_number": "13335",
                        "autonomous_system_organization": "Cloudflare",
                    }
                ],
            )

            with mock.patch.dict(
                os.environ,
                {
                    "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                },
                clear=False,
            ):
                rows = list(process._iter_asn_output_rows(csv_path))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["set_name"], "asn_v4_ranges")
        self.assertEqual(rows[0]["range_start_int"], str(int(process.ipaddress.ip_address("1.0.0.0"))))

    def test_iter_city_output_rows_uses_dataset_specific_set_names(self):
        with tempfile.TemporaryDirectory() as directory:
            city_path = os.path.join(directory, "GeoLite2-City-Blocks-IPv4.csv")
            _write_csv(
                city_path,
                ["network", "geoname_id", "registered_country_geoname_id"],
                [{"network": "1.0.0.0/31", "geoname_id": "100", "registered_country_geoname_id": ""}],
            )

            locations = {
                "100": {
                    "country_iso_code": "US",
                    "country_name": "United States",
                    "subdivision": "Ohio",
                    "city": "Columbus",
                }
            }

            with mock.patch.dict(
                os.environ,
                {
                    "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
                },
                clear=False,
            ):
                rows = list(process._iter_city_output_rows(city_path, locations))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["set_name"], "city_v4_ranges")
        self.assertEqual(rows[0]["country_iso_code"], "US")


class DownloadCleanupTests(unittest.TestCase):

    def test_cleanup_downloaded_sources_removes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "GeoLite2-ASN-Blocks-IPv4.csv")
            second = os.path.join(directory, "GeoLite2-City-Blocks-IPv4.csv")
            with open(first, "w", encoding="utf-8") as handle:
                handle.write("data")
            with open(second, "w", encoding="utf-8") as handle:
                handle.write("data")

            process._cleanup_downloaded_sources(
                directory,
                [
                    "GeoLite2-ASN-Blocks-IPv4.csv",
                    "GeoLite2-City-Blocks-IPv4.csv",
                    "does-not-exist.csv",
                ],
            )

            self.assertFalse(os.path.exists(first))
            self.assertFalse(os.path.exists(second))


class HandlerWorkerModeTests(unittest.TestCase):

    def test_handler_processes_source_job_with_valkey_context(self):
        lifecycle = {"pool_closed": False, "client_closed": False}

        class FakePool:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def aclose(self):
                lifecycle["pool_closed"] = True

        class FakeRedisClient:
            def __init__(self, connection_pool):
                self.connection_pool = connection_pool

            async def aclose(self):
                lifecycle["client_closed"] = True

        fake_redis_module = types.ModuleType("redis.asyncio")
        fake_redis_module.ConnectionPool = FakePool
        fake_redis_module.Redis = FakeRedisClient
        fake_redis_module.SSLConnection = object
        fake_redis_package = types.ModuleType("redis")
        fake_redis_package.asyncio = fake_redis_module

        event = {
            "Records": [
                {
                    "body": json.dumps(
                        {
                            "jobType": process.JOB_TYPE_SOURCE_BUILD,
                            "sourceKey": "GeoLite2-ASN-Blocks-IPv4.csv",
                        }
                    )
                }
            ]
        }

        captured = {"valkey_enabled": False}

        def _fake_process_source_job(
            s3_client,
            download_bucket_name,
            processed_bucket_name,
            source_key,
            valkey_context,
        ):
            del s3_client
            self.assertEqual(download_bucket_name, "download-bucket")
            self.assertEqual(processed_bucket_name, "processed-bucket")
            self.assertEqual(source_key, "GeoLite2-ASN-Blocks-IPv4.csv")
            self.assertIsNotNone(valkey_context)
            captured["valkey_enabled"] = valkey_context is not None
            return {
                "sourceKey": source_key,
                "output": process.SOURCE_OUTPUT_CONFIG[source_key]["output"],
                "outputRows": 1,
            }

        with mock.patch.dict(
            sys.modules,
            {
                "redis": fake_redis_package,
                "redis.asyncio": fake_redis_module,
            },
            clear=False,
        ), mock.patch.dict(
            os.environ,
            {
                "DOWNLOAD_BUCKET_NAME": "download-bucket",
                "PROCESSED_BUCKET_NAME": "processed-bucket",
                "VALKEY_ENDPOINT": "cache.example",
                "VALKEY_TLS": "true",
            },
            clear=False,
        ), mock.patch.object(
            process, "_boto3_client", return_value=object()
        ), mock.patch.object(
            process, "_process_source_job", side_effect=_fake_process_source_job
        ):
            result = process.handler(event, type("Ctx", (), {"aws_request_id": "req-1"})())

        self.assertTrue(captured["valkey_enabled"])
        self.assertTrue(lifecycle["pool_closed"])
        self.assertTrue(lifecycle["client_closed"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["mode"], "worker")
        self.assertTrue(result["valkeyEnabled"])
        self.assertEqual(result["outputs"], ["GeoLite2-ASN-Blocks-IPv4.txt"])


if __name__ == "__main__":
    unittest.main()

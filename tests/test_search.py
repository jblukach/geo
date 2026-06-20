import json
import os
import sys
import types
import unittest
from unittest import mock

from search import search


ATTRIBUTION_TEXT = "This product includes GeoLite2 data created by MaxMind, available from https://www.maxmind.com."


class SearchInputTests(unittest.TestCase):

    def test_input_ips_empty_when_not_provided(self):
        self.assertEqual(search._input_ips({}), [])

    def test_input_ips_supports_api_gateway_path_parameter(self):
        event = {
            "pathParameters": {
                "ip": "134.129.111.111",
            }
        }
        self.assertEqual(search._input_ips(event), ["134.129.111.111"])

    def test_input_ips_supports_raw_path(self):
        event = {
            "rawPath": "/geo/134.129.111.111",
        }
        self.assertEqual(search._input_ips(event), ["134.129.111.111"])

    def test_input_ips_prefers_query_string_parameters(self):
        event = {
            "queryStringParameters": {
                "ip": "1.1.1.1",
            }
        }
        self.assertEqual(search._input_ips(event), ["1.1.1.1"])

    def test_input_ips_accepts_list_and_csv(self):
        event = {
            "ips": ["1.1.1.1", "2.2.2.2"],
            "queryStringParameters": {
                "ip": "3.3.3.3,4.4.4.4",
            },
        }
        self.assertEqual(search._input_ips(event), ["3.3.3.3", "4.4.4.4", "1.1.1.1", "2.2.2.2"])

    def test_input_ips_supports_post_body_array(self):
        event = {
            "body": json.dumps(["1.1.1.1", "2001:db8::1"]),
        }
        self.assertEqual(search._input_ips(event), ["1.1.1.1", "2001:db8::1"])

    def test_input_ips_uses_request_source_ip_for_geo_get_when_empty(self):
        event = {
            "rawPath": "/geo",
            "requestContext": {
                "http": {
                    "method": "GET",
                    "sourceIp": "203.0.113.10",
                }
            },
        }
        self.assertEqual(search._input_ips(event), ["203.0.113.10"])

    def test_input_ips_does_not_use_request_source_ip_for_non_get(self):
        event = {
            "rawPath": "/geo",
            "requestContext": {
                "http": {
                    "method": "POST",
                    "sourceIp": "203.0.113.10",
                }
            },
        }
        self.assertEqual(search._input_ips(event), [])


class SearchLookupTests(unittest.TestCase):

    def test_lookup_member_uses_ipv4_set_name(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                self.calls.append((set_name, max_score, min_score, start, num, withscores))
                return ["16777217|asn|1.0.0.0/31|4|13335|Cloudflare"]

        client = FakeClient()
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
            },
            clear=False,
        ):
            result = search._lookup_member(client, "asn", "1.0.0.1")

        self.assertEqual(result["asn"], "13335")
        self.assertEqual(client.calls[0], ("asn_v4_ranges", 16777217, 0, 0, 1, False))

    def test_lookup_member_uses_ipv6_set_name(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                self.calls.append((set_name, max_score, min_score, start, num, withscores))
                return ["340282366920938463463374607431768211455|city|::/0|6|US|United States|Ohio|Columbus"]

        client = FakeClient()
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_CITY_V6_SET_NAME": "city_v6_ranges",
            },
            clear=False,
        ):
            result = search._lookup_member(client, "city", "2001:db8::1")

        self.assertEqual(result["ip_version"], "6")
        self.assertEqual(client.calls[0][0], "city_v6_ranges")


class SearchHandlerTests(unittest.TestCase):

    def test_handler_returns_400_when_no_ip_input(self):
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
            },
            clear=False,
        ):
            response = search.handler({}, None)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("At least one IP address is required", body["error"])

    def test_handler_returns_invalid_ip_entry_for_invalid_ip(self):
        event = {"ip": "not-an-ip"}
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
            },
            clear=False,
        ):
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["attribution"], ATTRIBUTION_TEXT)
        self.assertEqual(body["requested_count"], 1)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["ip"], "not-an-ip")
        self.assertIn("Invalid IP address", body["results"][0]["error"])

    def test_handler_with_single_ip_returns_results(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                self.calls.append((set_name, max_score, min_score, start, num, withscores))
                if "asn" in set_name:
                    return ["2266097663|asn|134.129.0.0/16|4|65000|Example Org"]
                return ["2266163199|city|134.129.0.0/16|4|US|United States|Ohio|Columbus"]

            def get(self, key):
                if key.endswith(":asn"):
                    return "2026-06-19T12:00:00Z"
                if key.endswith(":city"):
                    return "2026-06-19T12:05:00Z"
                return None

            def close(self):
                return None

        fake_redis_module = types.ModuleType("redis")
        captured = {"client": None}

        def _fake_redis_constructor(**kwargs):
            client = FakeRedisClient(**kwargs)
            captured["client"] = client
            return client

        fake_redis_module.Redis = _fake_redis_constructor

        with mock.patch.dict(
            sys.modules,
            {
                "redis": fake_redis_module,
            },
            clear=False,
        ), mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
                "VALKEY_TLS": "true",
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
            },
            clear=False,
        ):
            response = search.handler({"ip": "134.129.111.111"}, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["attribution"], ATTRIBUTION_TEXT)
        self.assertEqual(body["requested_count"], 1)
        self.assertEqual(body["geolite2-asn.csv"], "2026-06-19T12:00:00Z")
        self.assertEqual(body["geolite2-city.csv"], "2026-06-19T12:05:00Z")
        self.assertEqual(len(body["results"]), 1)
        result = body["results"][0]
        self.assertEqual(result["ip"], "134.129.111.111")
        self.assertEqual(result["asn"]["id"], 65000)
        self.assertEqual(result["asn"]["org"], "Example Org")
        self.assertEqual(result["asn"]["net"], "134.129.0.0/16")
        self.assertEqual(result["geo"]["country"], "United States - US")
        self.assertEqual(result["geo"]["state"], "Ohio")
        self.assertEqual(result["geo"]["city"], "Columbus")
        self.assertEqual(result["geo"]["cidr"], "134.129.0.0/16")
        self.assertEqual(captured["client"].kwargs["host"], "cache.example")

    def test_handler_accepts_multiple_ips(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                if "asn" in set_name:
                    return ["2266097663|asn|134.129.0.0/16|4|65000|Example Org"]
                return ["2266163199|city|134.129.0.0/16|4|US|United States|Ohio|Columbus"]

            def get(self, key):
                if key.endswith(":asn"):
                    return "2026-06-19T12:00:00Z"
                if key.endswith(":city"):
                    return "2026-06-19T12:05:00Z"
                return None

            def close(self):
                return None

        fake_redis_module = types.ModuleType("redis")
        fake_redis_module.Redis = lambda **kwargs: FakeRedisClient(**kwargs)

        with mock.patch.dict(
            sys.modules,
            {
                "redis": fake_redis_module,
            },
            clear=False,
        ), mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
                "VALKEY_TLS": "true",
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
            },
            clear=False,
        ):
            response = search.handler({"ips": ["134.129.111.111", "bad-ip"]}, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["attribution"], ATTRIBUTION_TEXT)
        self.assertEqual(body["requested_count"], 2)
        self.assertEqual(body["geolite2-asn.csv"], "2026-06-19T12:00:00Z")
        self.assertEqual(body["geolite2-city.csv"], "2026-06-19T12:05:00Z")
        self.assertEqual(len(body["results"]), 2)
        results_by_ip = {entry["ip"]: entry for entry in body["results"]}
        self.assertIn("asn", results_by_ip["134.129.111.111"])
        self.assertIn("geo", results_by_ip["134.129.111.111"])
        self.assertIn("error", results_by_ip["bad-ip"])

    def test_handler_rejects_request_over_max_bulk_limit(self):
        event = {"ips": ["1.1.1.1", "2.2.2.2", "3.3.3.3"]}
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
                "MAX_IPS_PER_REQUEST": "2",
            },
            clear=False,
        ):
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 400)
        body = json.loads(response["body"])
        self.assertIn("Too many IPs requested", body["error"])

    def test_handler_rejects_request_body_over_limit(self):
        oversized_body = json.dumps({"ips": ["1.1.1.1"]}) + ("x" * 1024)
        event = {"body": oversized_body}
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
                "MAX_REQUEST_BODY_BYTES": "64",
            },
            clear=False,
        ):
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 413)
        body = json.loads(response["body"])
        self.assertIn("Request body too large", body["error"])

    def test_handler_returns_503_when_remaining_time_too_low(self):
        class FakeContext:
            aws_request_id = "req-123"

            def get_remaining_time_in_millis(self):
                return 200

        event = {"ips": ["1.1.1.1"]}
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
                "MIN_REMAINING_TIME_MS": "500",
            },
            clear=False,
        ):
            response = search.handler(event, FakeContext())

        self.assertEqual(response["statusCode"], 503)
        body = json.loads(response["body"])
        self.assertIn("Insufficient processing time remaining", body["error"])

    def test_handler_deduplicates_valid_ip_lookups(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.lookup_calls = 0

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                self.lookup_calls += 1
                if "asn" in set_name:
                    return ["2266097663|asn|134.129.0.0/16|4|65000|Example Org"]
                return ["2266163199|city|134.129.0.0/16|4|US|United States|Ohio|Columbus"]

            def get(self, key):
                del key
                return None

            def close(self):
                return None

        fake_redis_module = types.ModuleType("redis")
        captured = {"client": None}

        def _fake_redis_constructor(**kwargs):
            client = FakeRedisClient(**kwargs)
            captured["client"] = client
            return client

        fake_redis_module.Redis = _fake_redis_constructor

        with mock.patch.dict(
            sys.modules,
            {
                "redis": fake_redis_module,
            },
            clear=False,
        ), mock.patch.dict(
            os.environ,
            {
                "VALKEY_ENDPOINT": "cache.example",
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
            },
            clear=False,
        ):
            response = search.handler({"ips": ["134.129.111.111", "134.129.111.111"]}, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertIsNotNone(captured["client"])
        self.assertEqual(captured["client"].lookup_calls, 2)

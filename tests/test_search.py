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
                "ip": "198.51.100.111",
            }
        }
        self.assertEqual(search._input_ips(event), ["198.51.100.111"])

    def test_input_ips_supports_raw_path(self):
        event = {
            "rawPath": "/geo/198.51.100.111",
        }
        self.assertEqual(search._input_ips(event), ["198.51.100.111"])

    def test_input_ips_raw_path_is_not_duplicated_when_path_parameters_present(self):
        event = {
            "rawPath": "/geo/198.51.100.111",
            "pathParameters": {"ip": "198.51.100.111"},
        }
        self.assertEqual(search._input_ips(event), ["198.51.100.111"])

    def test_input_ips_decodes_url_encoded_ipv6_from_raw_path(self):
        event = {
            "rawPath": "/geo/2001%3Adb8%3A%3A1",
        }
        self.assertEqual(search._input_ips(event), ["2001:db8::1"])

    def test_input_ips_prefers_query_string_parameters(self):
        event = {
            "queryStringParameters": {
                "ip": "192.0.2.1",
            }
        }
        self.assertEqual(search._input_ips(event), ["192.0.2.1"])

    def test_input_ips_supports_repeated_ip_query_params_from_raw_query_string(self):
        event = {
            "rawQueryString": "ip=192.0.2.1&ip=198.51.100.111&ip=2001%3Adb8%3A%3A1",
            "queryStringParameters": {"ip": "2001:db8::1"},
        }
        self.assertEqual(search._input_ips(event), ["192.0.2.1", "198.51.100.111", "2001:db8::1"])

    def test_input_ips_prefers_raw_query_string_over_single_query_value(self):
        event = {
            "rawQueryString": "ip=192.0.2.1&ip=198.51.100.111",
            "queryStringParameters": {"ip": "198.51.100.111"},
        }
        self.assertEqual(search._input_ips(event), ["192.0.2.1", "198.51.100.111"])

    def test_input_ips_supports_multi_value_query_string_parameters(self):
        event = {
            "multiValueQueryStringParameters": {
                "ip": ["192.0.2.1", "2001:db8::1"],
            }
        }
        self.assertEqual(search._input_ips(event), ["192.0.2.1", "2001:db8::1"])

    def test_input_ips_accepts_list_and_csv(self):
        event = {
            "ips": ["192.0.2.1", "192.0.2.2"],
            "queryStringParameters": {
                "ip": "203.0.113.3,203.0.113.4",
            },
        }
        self.assertEqual(search._input_ips(event), ["203.0.113.3", "203.0.113.4", "192.0.2.1", "192.0.2.2"])

    def test_input_ips_supports_post_body_array(self):
        event = {
            "body": json.dumps(["192.0.2.1", "2001:db8::1"]),
        }
        self.assertEqual(search._input_ips(event), ["192.0.2.1", "2001:db8::1"])

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
                return ["3221225985|asn|192.0.2.0/31|4|13335|Cloudflare"]

        client = FakeClient()
        with mock.patch.dict(
            os.environ,
            {
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
            },
            clear=False,
        ):
            result = search._lookup_member(client, "asn", "192.0.2.1")

        self.assertEqual(result["asn"], "13335")
        self.assertEqual(client.calls[0], ("asn_v4_ranges", 3221225985, 0, 0, 1, False))

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
                    return ["3325256959|asn|198.51.100.0/24|4|65000|Example Org"]
                return ["3325256959|city|198.51.100.0/24|4|US|United States|Ohio|Columbus"]

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
            response = search.handler({"ip": "198.51.100.111"}, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["attribution"], ATTRIBUTION_TEXT)
        self.assertEqual(body["requested_count"], 1)
        self.assertEqual(body["geolite2-asn.csv"], "2026-06-19T12:00:00Z")
        self.assertEqual(body["geolite2-city.csv"], "2026-06-19T12:05:00Z")
        self.assertEqual(len(body["results"]), 1)
        result = body["results"][0]
        self.assertEqual(result["ip"], "198.51.100.111")
        self.assertEqual(result["asn"]["id"], 65000)
        self.assertEqual(result["asn"]["org"], "Example Org")
        self.assertEqual(result["asn"]["net"], "198.51.100.0/24")
        self.assertEqual(result["geo"]["country"], "United States - US")
        self.assertEqual(result["geo"]["state"], "Ohio")
        self.assertEqual(result["geo"]["city"], "Columbus")
        self.assertEqual(result["geo"]["cidr"], "198.51.100.0/24")
        self.assertEqual(captured["client"].kwargs["host"], "cache.example")

    def test_handler_path_parameter_and_raw_path_single_ip_returns_one_result(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.lookup_calls = 0

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                self.lookup_calls += 1
                if "asn" in set_name:
                    return ["3325256959|asn|198.51.100.0/24|4|65000|Example Org"]
                return ["3325256959|city|198.51.100.0/24|4|US|United States|Ohio|Columbus"]

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

        event = {
            "rawPath": "/geo/198.51.100.111",
            "pathParameters": {"ip": "198.51.100.111"},
        }
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
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["requested_count"], 1)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["ip"], "198.51.100.111")
        self.assertEqual(captured["client"].lookup_calls, 2)

    def test_handler_url_encoded_ipv6_in_path_returns_single_result(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                self.calls.append(set_name)
                if "asn" in set_name:
                    return ["42540766411282592856903984951653826561|asn|2001:db8::/32|6|65001|Example IPv6 Org"]
                return ["42540766490510755371168322545197776895|city|2001:db8::/32|6|US|United States|Ohio|Columbus"]

            def get(self, key):
                del key
                return None

            def close(self):
                return None

        fake_redis_module = types.ModuleType("redis")
        fake_redis_module.Redis = lambda **kwargs: FakeRedisClient(**kwargs)

        event = {
            "rawPath": "/geo/2001%3Adb8%3A%3A1",
        }
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
                "VALKEY_ASN_V6_SET_NAME": "asn_v6_ranges",
                "VALKEY_CITY_V6_SET_NAME": "city_v6_ranges",
            },
            clear=False,
        ):
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["requested_count"], 1)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["ip"], "2001:db8::1")
        self.assertNotIn("error", body["results"][0])

    def test_handler_get_geo_without_input_uses_source_ip(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                if "asn" in set_name:
                    return ["3405804031|asn|203.0.113.0/24|4|65010|Example Source Org"]
                return ["3405804031|city|203.0.113.0/24|4|US|United States|Ohio|Columbus"]

            def get(self, key):
                del key
                return None

            def close(self):
                return None

        fake_redis_module = types.ModuleType("redis")
        fake_redis_module.Redis = lambda **kwargs: FakeRedisClient(**kwargs)

        event = {
            "rawPath": "/geo",
            "requestContext": {
                "http": {
                    "method": "GET",
                    "sourceIp": "203.0.113.10",
                }
            },
        }
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
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["requested_count"], 1)
        self.assertEqual(body["results"][0]["ip"], "203.0.113.10")

    def test_handler_accepts_multiple_ips(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                if "asn" in set_name:
                    return ["3325256959|asn|198.51.100.0/24|4|65000|Example Org"]
                return ["3325256959|city|198.51.100.0/24|4|US|United States|Ohio|Columbus"]

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
            response = search.handler({"ips": ["198.51.100.111", "bad-ip"]}, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["attribution"], ATTRIBUTION_TEXT)
        self.assertEqual(body["requested_count"], 2)
        self.assertEqual(body["geolite2-asn.csv"], "2026-06-19T12:00:00Z")
        self.assertEqual(body["geolite2-city.csv"], "2026-06-19T12:05:00Z")
        self.assertEqual(len(body["results"]), 2)
        results_by_ip = {entry["ip"]: entry for entry in body["results"]}
        self.assertIn("asn", results_by_ip["198.51.100.111"])
        self.assertIn("geo", results_by_ip["198.51.100.111"])
        self.assertIn("error", results_by_ip["bad-ip"])

    def test_handler_rejects_request_over_max_bulk_limit(self):
        event = {"ips": ["192.0.2.1", "192.0.2.2", "203.0.113.3"]}
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
        oversized_body = json.dumps({"ips": ["192.0.2.1"]}) + ("x" * 1024)
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

        event = {"ips": ["192.0.2.1"]}
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
                    return ["3325256959|asn|198.51.100.0/24|4|65000|Example Org"]
                return ["3325256959|city|198.51.100.0/24|4|US|United States|Ohio|Columbus"]

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
            response = search.handler({"ips": ["198.51.100.111", "198.51.100.111"]}, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertIsNotNone(captured["client"])
        self.assertEqual(captured["client"].lookup_calls, 2)

    def test_handler_accepts_mixed_ipv4_ipv6_up_to_limit_and_deduplicates_lookups(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.lookup_calls = 0
                self.set_names: list[str] = []

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                self.lookup_calls += 1
                self.set_names.append(set_name)
                if set_name == "asn_v4_ranges":
                    return ["3325256959|asn|198.51.100.0/24|4|13335|Cloudflare"]
                if set_name == "city_v4_ranges":
                    return ["3325256959|city|198.51.100.0/24|4|US|United States|California|Los Angeles"]
                if set_name == "asn_v6_ranges":
                    return ["42540766411282592856903984951653826561|asn|2001:db8::/32|6|65001|Example IPv6 Org"]
                return ["42540766490510755371168322545197776895|city|2001:db8::/32|6|US|United States|Ohio|Columbus"]

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

        event = {
            "ips": [
                "192.0.2.1",
                "2001:db8::1",
                "192.0.2.1",
                "2001:db8::1",
            ]
        }
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
                "MAX_IPS_PER_REQUEST": "4",
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
                "VALKEY_ASN_V6_SET_NAME": "asn_v6_ranges",
                "VALKEY_CITY_V6_SET_NAME": "city_v6_ranges",
            },
            clear=False,
        ):
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["requested_count"], 4)
        self.assertEqual(len(body["results"]), 4)
        self.assertIsNotNone(captured["client"])
        self.assertEqual(captured["client"].lookup_calls, 4)
        self.assertCountEqual(
            captured["client"].set_names,
            ["asn_v4_ranges", "city_v4_ranges", "asn_v6_ranges", "city_v6_ranges"],
        )

    def test_handler_accepts_post_body_with_exactly_300_ips(self):
        class FakeRedisClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def zrevrangebyscore(self, set_name, max_score, min_score, start, num, withscores):
                del max_score, min_score, start, num, withscores
                if "asn" in set_name:
                    return ["167772415|asn|10.0.0.0/8|4|65000|Example Org"]
                return ["184549375|city|10.0.0.0/8|4|US|United States|Ohio|Columbus"]

            def get(self, key):
                del key
                return None

            def close(self):
                return None

        fake_redis_module = types.ModuleType("redis")
        fake_redis_module.Redis = lambda **kwargs: FakeRedisClient(**kwargs)

        ip_list = [f"10.0.{idx // 256}.{idx % 256}" for idx in range(300)]
        event = {
            "body": json.dumps({"ips": ip_list}),
        }

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
                "MAX_IPS_PER_REQUEST": "300",
                "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
            },
            clear=False,
        ):
            response = search.handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["requested_count"], 300)
        self.assertEqual(len(body["results"]), 300)

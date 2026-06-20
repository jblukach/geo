import unittest

import aws_cdk as cdk
from aws_cdk import assertions

from geo.geo_network import GeoNetworkStack
from geo.geo_search import GeoSearchStack


class GeoSearchStackTests(unittest.TestCase):

    def test_geo_search_stack_lambda_configuration(self):
        app = cdk.App()
        network_stack = GeoNetworkStack(app, "GeoNetworkStackTest")
        search_stack = GeoSearchStack(
            app,
            "GeoSearchStackTest",
            vpc=network_stack.vpc,
            process_security_group=network_stack.process_security_group,
            valkey_endpoint="cache.example",
            valkey_port="6379",
        )

        template = assertions.Template.from_stack(search_stack)

        template.has_resource_properties(
            "AWS::Lambda::LayerVersion",
            {
                "CompatibleArchitectures": ["arm64"],
                "CompatibleRuntimes": ["python3.13"],
                "Content": {
                    "S3Bucket": "packages-use2-lukach-io",
                    "S3Key": "redis.zip",
                },
                "LayerName": "redis",
            },
        )

        template.has_resource_properties(
            "AWS::Lambda::Function",
            {
                "FunctionName": "geo-search",
                "Runtime": "python3.13",
                "Handler": "search.handler",
                "MemorySize": 1024,
                "Timeout": 30,
                "Environment": {
                    "Variables": {
                        "VALKEY_ENDPOINT": "cache.example",
                        "VALKEY_PORT": "6379",
                        "VALKEY_TLS": "true",
                        "VALKEY_ASN_V4_SET_NAME": "asn_v4_ranges",
                        "VALKEY_ASN_V6_SET_NAME": "asn_v6_ranges",
                        "VALKEY_CITY_V4_SET_NAME": "city_v4_ranges",
                        "VALKEY_CITY_V6_SET_NAME": "city_v6_ranges",
                        "VALKEY_LAST_UPDATED_ASN_KEY": "geo:last_updated:asn",
                        "VALKEY_LAST_UPDATED_CITY_KEY": "geo:last_updated:city",
                        "MAX_IPS_PER_REQUEST": "300",
                        "MAX_REQUEST_BODY_BYTES": "262144",
                        "MIN_REMAINING_TIME_MS": "1500",
                    }
                },
                "VpcConfig": assertions.Match.object_like(
                    {
                        "SecurityGroupIds": assertions.Match.any_value(),
                        "SubnetIds": assertions.Match.any_value(),
                    }
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()

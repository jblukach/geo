from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as _ec2
from aws_cdk import aws_iam as _iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as _logs
from aws_cdk import aws_s3 as _s3
from aws_cdk import aws_ssm as _ssm
from constructs import Construct

import config


class GeoSearchStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: _ec2.IVpc,
        process_security_group: _ec2.ISecurityGroup,
        valkey_endpoint: str,
        valkey_port: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        package_bucket = _s3.Bucket.from_bucket_name(
            self,
            "packagelayerbucket",
            bucket_name="packages-use2-lukach-io",
        )

        redis_layer = _lambda.LayerVersion(
            self,
            "redislayer",
            layer_version_name="redis",
            code=_lambda.Code.from_bucket(
                bucket=package_bucket,
                key="redis.zip",
            ),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        search = _lambda.Function(
            self,
            "search",
            function_name="geo-search",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset("search"),
            handler="search.handler",
            timeout=Duration.seconds(30),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=_ec2.SubnetSelection(subnet_type=_ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[process_security_group],
            layers=[redis_layer],
            environment={
                "VALKEY_ENDPOINT": valkey_endpoint,
                "VALKEY_PORT": valkey_port,
                "VALKEY_TLS": str(config.VALKEY_TLS).lower(),
                "VALKEY_ASN_V4_SET_NAME": config.VALKEY_ASN_V4_SET_NAME,
                "VALKEY_ASN_V6_SET_NAME": config.VALKEY_ASN_V6_SET_NAME,
                "VALKEY_CITY_V4_SET_NAME": config.VALKEY_CITY_V4_SET_NAME,
                "VALKEY_CITY_V6_SET_NAME": config.VALKEY_CITY_V6_SET_NAME,
                "VALKEY_LAST_UPDATED_ASN_KEY": config.VALKEY_LAST_UPDATED_ASN_KEY,
                "VALKEY_LAST_UPDATED_CITY_KEY": config.VALKEY_LAST_UPDATED_CITY_KEY,
                "MAX_IPS_PER_REQUEST": str(config.SEARCH_MAX_IPS_PER_REQUEST),
                "MAX_REQUEST_BODY_BYTES": str(config.SEARCH_MAX_REQUEST_BODY_BYTES),
                "MIN_REMAINING_TIME_MS": str(config.SEARCH_MIN_REMAINING_TIME_MS),
            },
        )

        apigateway = _ssm.StringParameter.from_string_parameter_attributes(
            self,
            "apigateway",
            parameter_name="/account/api",
        )

        composite = _iam.CompositePrincipal(
            _iam.AccountPrincipal(apigateway.string_value),
            _iam.ServicePrincipal("apigateway.amazonaws.com"),
        )

        search.grant_invoke_composite_principal(composite)

        _logs.LogGroup(
            self,
            "searchlogs",
            log_group_name="/aws/lambda/" + search.function_name,
            retention=_logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

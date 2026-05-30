from aws_cdk import (
    Duration,
    RemovalPolicy,
    Size,
    aws_events as _events,
    aws_events_targets as _targets,
    aws_iam as _iam,
    aws_lambda as _lambda,
    aws_logs as _logs,
    aws_secretsmanager as _secrets,
    aws_s3 as _s3,
    aws_ssm as _ssm,
)

from constructs import Construct


class GeoDownload(Construct):

    def __init__(self, scope: Construct, construct_id: str, download_bucket: _s3.IBucket, **kwargs) -> None:
        super().__init__(scope, construct_id)

        _ssm.StringParameter(
            self,
            "asncsvparameter",
            parameter_name="/maxmind/geolite2/asn-csv",
            string_value="EMPTY",
            description="MaxMind GeoLite2 ASN CSV Last Updated",
            tier=_ssm.ParameterTier.STANDARD,
        )

        _ssm.StringParameter(
            self,
            "citycsvparameter",
            parameter_name="/maxmind/geolite2/city-csv",
            string_value="EMPTY",
            description="MaxMind GeoLite2 City CSV Last Updated",
            tier=_ssm.ParameterTier.STANDARD,
        )

        role = _iam.Role(
            self,
            "downloadrole",
            assumed_by=_iam.ServicePrincipal("lambda.amazonaws.com"),
        )

        role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        )

        role.add_to_policy(
            _iam.PolicyStatement(
                actions=[
                    "ssm:GetParameter",
                    "ssm:PutParameter",
                ],
                resources=["*"],
            )
        )

        download_bucket.grant_put(role)

        secret = _secrets.Secret.from_secret_name_v2(
            self,
            "credentialssecret",
            secret_name="credentials",
        )
        secret.grant_read(role)

        download = _lambda.Function(
            self,
            "download",
            function_name="geolite2-download",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset("download/geolite2"),
            handler="geolite2.handler",
            ephemeral_storage_size = Size.gibibytes(2),
            timeout=Duration.seconds(900),
            memory_size=2048,
            role=role,
            environment={
                "SECRET_NAME": "credentials",
                "DOWNLOAD_BUCKET_NAME": download_bucket.bucket_name,
                "SSM_PARAMETER_ASN_CSV": "/maxmind/geolite2/asn-csv",
                "SSM_PARAMETER_CITY_CSV": "/maxmind/geolite2/city-csv",
            },
        )

        _logs.LogGroup(
            self,
            "downloadlogs",
            log_group_name="/aws/lambda/" + download.function_name,
            retention=_logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        dbip_download = _lambda.Function(
            self,
            "dbipdownload",
            function_name="dbip-download",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset("download/dbip"),
            handler="dbip.handler",
            ephemeral_storage_size=Size.gibibytes(2),
            timeout=Duration.seconds(900),
            memory_size=2048,
            role=role,
            environment={
                "DOWNLOAD_BUCKET_NAME": download_bucket.bucket_name,
            },
        )

        ipinfo_download = _lambda.Function(
            self,
            "ipinfodownload",
            function_name="ipinfo-download",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset("download/ipinfo"),
            handler="ipinfo.handler",
            ephemeral_storage_size=Size.gibibytes(2),
            timeout=Duration.seconds(900),
            memory_size=2048,
            role=role,
            environment={
                "SECRET_NAME": "credentials",
                "IPINFO_SECRET_KEY": "IPINFO",
                "DOWNLOAD_BUCKET_NAME": download_bucket.bucket_name,
            },
        )

        _logs.LogGroup(
            self,
            "dbipdownloadlogs",
            log_group_name="/aws/lambda/" + dbip_download.function_name,
            retention=_logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        _logs.LogGroup(
            self,
            "ipinfodownloadlogs",
            log_group_name="/aws/lambda/" + ipinfo_download.function_name,
            retention=_logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        event = _events.Rule(
            self,
            "downloadevent",
            schedule=_events.Schedule.cron(
                minute="0",
                hour="*",
                month="*",
                week_day="*",
                year="*",
            ),
        )

        event.add_target(_targets.LambdaFunction(download))

        dbip_event = _events.Rule(
            self,
            "dbipdownloadevent",
            schedule=_events.Schedule.cron(
                minute="0",
                hour="12",
                day="1",
                month="*",
                year="*",
            ),
        )

        dbip_event.add_target(_targets.LambdaFunction(dbip_download))

        ipinfo_event = _events.Rule(
            self,
            "ipinfodownloadevent",
            schedule=_events.Schedule.cron(
                minute="0",
                hour="12",
                day="*",
                month="*",
                year="*",
            ),
        )

        ipinfo_event.add_target(_targets.LambdaFunction(ipinfo_download))

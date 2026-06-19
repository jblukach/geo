import json
import os

from aws_cdk import Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_events as _events
from aws_cdk import aws_events_targets as _targets
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as _event_sources
from aws_cdk import aws_logs as _logs
from aws_cdk import aws_s3 as _s3
from aws_cdk import aws_secretsmanager as _secrets
from aws_cdk import aws_sqs as _sqs
from constructs import Construct

import config


class GeoProcessStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        download_bucket_name: str,
        processed_bucket_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        download_bucket = _s3.Bucket.from_bucket_name(
            self,
            "downloadbucket",
            download_bucket_name,
        )
        processed_bucket = _s3.Bucket.from_bucket_name(
            self,
            "processedbucket",
            processed_bucket_name,
        )

        dead_letter_queue = _sqs.Queue(
            self,
            "processdlq",
            queue_name="geo-process-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )

        process_queue = _sqs.Queue(
            self,
            "processqueue",
            queue_name="geo-process",
            visibility_timeout=Duration.minutes(16),
            retention_period=Duration.days(4),
            dead_letter_queue=_sqs.DeadLetterQueue(
                queue=dead_letter_queue,
                max_receive_count=5,
            ),
            enforce_ssl=True,
        )

        package_bucket = _s3.Bucket.from_bucket_name(
            self,
            "packagelayerbucket",
            bucket_name="packages-use2-lukach-io",
        )

        momento_layer = _lambda.LayerVersion(
            self,
            "momentolayer",
            layer_version_name="momento",
            code=_lambda.Code.from_bucket(
                bucket=package_bucket,
                key="momento.zip",
            ),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        credentials_secret = _secrets.Secret.from_secret_name_v2(
            self,
            "processcredentialssecret",
            secret_name="credentials",
        )

        process = _lambda.Function(
            self,
            "process",
            function_name="geolite2-process",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            code=_lambda.Code.from_asset("process"),
            handler="process.handler",
            ephemeral_storage_size=Size.gibibytes(3),
            timeout=Duration.seconds(900),
            memory_size=3008,
            layers=[momento_layer],
            environment={
                "DOWNLOAD_BUCKET_NAME": download_bucket.bucket_name,
                "PROCESSED_BUCKET_NAME": processed_bucket_name,
                "PROCESS_QUEUE_URL": process_queue.queue_url,
                "MOMENTO_SECRET_NAME": "credentials",
                "MOMENTO_SECRET_KEY": "MOMENTO",
                "MOMENTO_ENDPOINT_SECRET_KEY": "MOMENTO_ENDPOINT",
                "MOMENTO_ENDPOINT": config.MOMENTO_ENDPOINT,
                "MOMENTO_SORTED_SET_BATCH_SIZE": str(config.MOMENTO_SORTED_SET_BATCH_SIZE),
                "MOMENTO_CACHE_NAMES_BY_SOURCE": json.dumps(
                    config.MOMENTO_CACHE_NAMES_BY_SOURCE,
                    sort_keys=True,
                ),
                "MOMENTO_SET_PREFIX": "geo",
                "MOMENTO_RELEASE": os.getenv("MOMENTO_RELEASE", ""),
            },
        )

        _logs.LogGroup(
            self,
            "processlogs",
            log_group_name="/aws/lambda/" + process.function_name,
            retention=_logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        download_bucket.grant_read(process)
        processed_bucket.grant_put(process)
        process_queue.grant_consume_messages(process)
        process_queue.grant_send_messages(process)
        credentials_secret.grant_read(process)

        process.add_event_source(
            _event_sources.SqsEventSource(
                process_queue,
                batch_size=1,
                report_batch_item_failures=True,
            )
        )

        object_created_rule = _events.Rule(
            self,
            "downloadobjectcreated",
            event_pattern=_events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {
                        "name": [download_bucket_name],
                    },
                    "object": {
                        "key": [
                            {"suffix": "GeoLite2-ASN-Blocks-IPv4.csv"},
                            {"suffix": "GeoLite2-ASN-Blocks-IPv6.csv"},
                            {"suffix": "GeoLite2-City-Blocks-IPv4.csv"},
                            {"suffix": "GeoLite2-City-Blocks-IPv6.csv"},
                        ],
                    },
                },
            ),
        )

        object_created_rule.add_target(_targets.SqsQueue(process_queue))

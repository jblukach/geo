from aws_cdk import Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_events as _events
from aws_cdk import aws_events_targets as _targets
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as _event_sources
from aws_cdk import aws_logs as _logs
from aws_cdk import aws_s3 as _s3
from aws_cdk import aws_sqs as _sqs
from constructs import Construct


class GeoProcessStack(Stack):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        download_bucket: _s3.IBucket,
        processed_bucket: _s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

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
            environment={
                "DOWNLOAD_BUCKET_NAME": download_bucket.bucket_name,
                "PROCESSED_BUCKET_NAME": processed_bucket.bucket_name,
                "PROCESS_QUEUE_URL": process_queue.queue_url,
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
                        "name": [download_bucket.bucket_name],
                    },
                    "object": {
                        "key": [
                            "GeoLite2-ASN-Blocks-IPv4.csv",
                            "GeoLite2-ASN-Blocks-IPv6.csv",
                            "GeoLite2-City-Blocks-IPv4.csv",
                            "GeoLite2-City-Blocks-IPv6.csv",
                        ],
                    },
                },
            ),
        )

        object_created_rule.add_target(_targets.SqsQueue(process_queue))

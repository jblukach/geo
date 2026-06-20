import datetime

from aws_cdk import aws_lambda as _lambda, aws_s3 as _s3

from constructs import Construct


class GeoLayer(Construct):

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
    ) -> None:
        super().__init__(scope, construct_id)

        year = datetime.datetime.now().strftime('%Y')
        month = datetime.datetime.now().strftime('%m')
        day = datetime.datetime.now().strftime('%d')

        bucket = _s3.Bucket.from_bucket_name(
            self,
            'bucket',
            bucket_name='packages-use2-lukach-io',
        )

        self.requests_layer = _lambda.LayerVersion(
            self,
            'requests',
            layer_version_name='requests',
            description=str(year) + '-' + str(month) + '-' + str(day) + ' deployment',
            code=_lambda.Code.from_bucket(
                bucket=bucket,
                key='requests.zip',
            ),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        self.redis_layer = _lambda.LayerVersion(
            self,
            'redis',
            layer_version_name='redis',
            description=str(year) + '-' + str(month) + '-' + str(day) + ' deployment',
            code=_lambda.Code.from_bucket(
                bucket=bucket,
                key='redis.zip',
            ),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_13],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )
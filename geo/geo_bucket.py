from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_s3 as _s3
)

from constructs import Construct


class GeoBucket(Construct):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id)

        account = Stack.of(self).account
        region = Stack.of(self).region

        self.download_bucket = _s3.Bucket(
            self,
            'download',
            bucket_name=f'geo-download-{region}-{account}',
            encryption=_s3.BucketEncryption.S3_MANAGED,
            block_public_access=_s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
            versioned=False,
        )

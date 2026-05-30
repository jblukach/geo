from aws_cdk import Stack
from constructs import Construct

from geo.geo_bucket import GeoBucket
from geo.geo_download import GeoDownload
from geo.geo_oidc import GeoOidc
from geo.geo_secret import GeoSecret


class GeoStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Deploy resource stacks under this app for storage, CI auth, and credentials.
        bucket = GeoBucket(self, 'GeoBucket')
        GeoOidc(self, 'GeoOidc')
        GeoSecret(self, 'GeoSecret')
        GeoDownload(self, 'GeoDownload', download_bucket=bucket.download_bucket)

from aws_cdk import (
    SecretValue,
    aws_secretsmanager as _secrets
)

from constructs import Construct


class GeoSecret(Construct):

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        _secrets.Secret(
            self,
            'credentials',
            secret_name='credentials',
            secret_object_value={
                'GEOLITE_API': SecretValue.unsafe_plain_text(''),
                'GEOLITE_KEY': SecretValue.unsafe_plain_text(''),
                'IP2LOCATION': SecretValue.unsafe_plain_text(''),
                'IPINFO': SecretValue.unsafe_plain_text(''),
            }
        )

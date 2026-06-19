#!/usr/bin/env python3
import os

import aws_cdk as cdk

from geo.geo_process import GeoProcessStack  # type: ignore[attr-defined]
from geo.geo_stack import GeoStack  # type: ignore[attr-defined]


app = cdk.App()
account = os.getenv('CDK_DEFAULT_ACCOUNT')
region = 'us-east-2'

geo_stack = GeoStack(
    app,
    "GeoStack",
    env=cdk.Environment(
        account=account,
        region=region
    ),
    synthesizer=cdk.DefaultStackSynthesizer(
        qualifier='lukach'
    )
)

geo_process_stack = GeoProcessStack(
    app,
    "GeoProcessStack",
    download_bucket_name=f'geo-download-{region}-{account}',
    processed_bucket_name=f'geo-processed-{region}-{account}',
    env=cdk.Environment(
        account=account,
        region=region
    ),
    synthesizer=cdk.DefaultStackSynthesizer(
        qualifier='lukach'
    )
)

# Ensure migration away from cross-stack bucket exports updates GeoProcessStack first.
geo_stack.add_dependency(geo_process_stack)

cdk.Tags.of(app).add('Alias', 'geo')
cdk.Tags.of(app).add('GitHub', 'https://github.com/jblukach/geo')
cdk.Tags.of(app).add('Org', 'lukach.io')

app.synth()

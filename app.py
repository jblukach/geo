#!/usr/bin/env python3
import os

import aws_cdk as cdk

from geo.geo_stack import GeoStack


app = cdk.App()
GeoStack(
    app,
    "GeoStack",
    env=cdk.Environment(
        account=os.getenv('CDK_DEFAULT_ACCOUNT'),
        region='us-east-2'
    ),
    synthesizer=cdk.DefaultStackSynthesizer(
        qualifier='lukach'
    )
)

cdk.Tags.of(app).add('Alias', 'geo')
cdk.Tags.of(app).add('GitHub', 'https://github.com/jblukach/geo')
cdk.Tags.of(app).add('Org', 'lukach.io')

app.synth()

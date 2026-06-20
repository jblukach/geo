#!/usr/bin/env python3
import os

import aws_cdk as cdk

from geo.geo_process import GeoProcessStack  # type: ignore[attr-defined]
from geo.geo_network import GeoNetworkStack  # type: ignore[attr-defined]
from geo.geo_search import GeoSearchStack  # type: ignore[attr-defined]
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

geo_network_stack = GeoNetworkStack(
    app,
    "GeoNetworkStack",
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
    vpc=geo_network_stack.vpc,
    process_security_group=geo_network_stack.process_security_group,
    valkey_endpoint=geo_network_stack.valkey_endpoint,
    valkey_port=geo_network_stack.valkey_port,
    env=cdk.Environment(
        account=account,
        region=region
    ),
    synthesizer=cdk.DefaultStackSynthesizer(
        qualifier='lukach'
    )
)

geo_search_stack = GeoSearchStack(
    app,
    "GeoSearchStack",
    vpc=geo_network_stack.vpc,
    process_security_group=geo_network_stack.process_security_group,
    valkey_endpoint=geo_network_stack.valkey_endpoint,
    valkey_port=geo_network_stack.valkey_port,
    env=cdk.Environment(
        account=account,
        region=region
    ),
    synthesizer=cdk.DefaultStackSynthesizer(
        qualifier='lukach'
    )
)

geo_process_stack.add_dependency(geo_network_stack)
geo_search_stack.add_dependency(geo_network_stack)

# Ensure migration away from cross-stack bucket exports updates GeoProcessStack first.
geo_stack.add_dependency(geo_process_stack)

cdk.Tags.of(app).add('Alias', 'geo')
cdk.Tags.of(app).add('GitHub', 'https://github.com/jblukach/geo')
cdk.Tags.of(app).add('Org', 'lukach.io')

app.synth()

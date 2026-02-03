import datetime

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as _iam,
    aws_lambda as _lambda,
    aws_logs as _logs,
    aws_s3 as _s3,
    aws_ssm as _ssm
)

from constructs import Construct

class GeoliteSearchUSW2(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        year = datetime.datetime.now().strftime('%Y')
        month = datetime.datetime.now().strftime('%m')
        day = datetime.datetime.now().strftime('%d')

    ### PARAMETERS ###

        organization = _ssm.StringParameter.from_string_parameter_attributes(
            self, 'organization',
            parameter_name = '/organization/id'
        )

    ### S3 BUCKET ###

        bucket = _s3.Bucket.from_bucket_name(
            self, 'bucket',
            bucket_name = 'packages-usw2-lukach-io'
        )

        staged = _s3.Bucket(
            self, 'staged',
            bucket_name = 'geolite-staged-usw2-lukach-io',
            encryption = _s3.BucketEncryption.S3_MANAGED,
            block_public_access = _s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy = RemovalPolicy.DESTROY,
            auto_delete_objects = True,
            enforce_ssl = True,
            versioned = False
        )

    ### LAMBDA LAYERS ###

        geoip2 = _lambda.LayerVersion(
            self, 'geoip2',
            layer_version_name = 'geoip2',
            description = str(year)+'-'+str(month)+'-'+str(day)+' deployment',
            code = _lambda.Code.from_bucket(
                bucket = bucket,
                key = 'geoip2.zip'
            ),
            compatible_architectures = [
                _lambda.Architecture.ARM_64
            ],
            compatible_runtimes = [
                _lambda.Runtime.PYTHON_3_13
            ],
            removal_policy = RemovalPolicy.DESTROY
        )

        maxminddb = _lambda.LayerVersion(
            self, 'maxminddb',
            layer_version_name = 'maxminddb',
            description = str(year)+'-'+str(month)+'-'+str(day)+' deployment',
            code = _lambda.Code.from_bucket(
                bucket = bucket,
                key = 'maxminddb.zip'
            ),
            compatible_architectures = [
                _lambda.Architecture.ARM_64
            ],
            compatible_runtimes = [
                _lambda.Runtime.PYTHON_3_13
            ],
            removal_policy = RemovalPolicy.DESTROY
        )

    ### IAM ROLE ###

        role = _iam.Role(
            self, 'role',
            assumed_by = _iam.ServicePrincipal(
                'lambda.amazonaws.com'
            )
        )

        role.add_managed_policy(
            _iam.ManagedPolicy.from_aws_managed_policy_name(
                'service-role/AWSLambdaBasicExecutionRole'
            )
        )

        role.add_to_policy(
            _iam.PolicyStatement(
                actions = [
                    'apigateway:GET'
                ],
                resources = [
                    '*'
                ]
            )
        )

    ### LAMBDA FUNCTION ###

        search = _lambda.Function(
            self, 'search',
            function_name = 'search',
            runtime = _lambda.Runtime.PYTHON_3_13,
            architecture = _lambda.Architecture.ARM_64,
            code = _lambda.Code.from_asset('search'),
            handler = 'search.handler',
            timeout = Duration.seconds(7),
            memory_size = 128,
            role = role,
            layers = [
                geoip2,
                maxminddb
            ]
        )

        composite = _iam.CompositePrincipal(
            _iam.OrganizationPrincipal(organization.string_value),
            _iam.ServicePrincipal('apigateway.amazonaws.com')
        )

        search.grant_invoke_composite_principal(composite)

        logs = _logs.LogGroup(
            self, 'logs',
            log_group_name = '/aws/lambda/'+search.function_name,
            retention = _logs.RetentionDays.ONE_WEEK,
            removal_policy = RemovalPolicy.DESTROY
        )

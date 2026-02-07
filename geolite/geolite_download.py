import datetime

from aws_cdk import (
    Duration,
    RemovalPolicy,
    SecretValue,
    Size,
    Stack,
    aws_events as _events,
    aws_events_targets as _targets,
    aws_iam as _iam,
    aws_lambda as _lambda,
    aws_logs as _logs,
    aws_s3 as _s3,
    aws_s3_deployment as _deployment,
    aws_secretsmanager as _secrets,
    aws_ssm as _ssm
)

from constructs import Construct

class GeoliteDownload(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = Stack.of(self).account

        year = datetime.datetime.now().strftime('%Y')
        month = datetime.datetime.now().strftime('%m')
        day = datetime.datetime.now().strftime('%d')

    ### PARAMETERS ###

        organization = _ssm.StringParameter.from_string_parameter_attributes(
            self, 'organization',
            parameter_name = '/organization/id'
        )

        asnparameter = _ssm.StringParameter(
            self, 'asnparameter',
            parameter_name = '/maxmind/geolite2/asn',
            string_value = 'EMPTY',
            description = 'MaxMind GeoLite2 ASN Last Updated',
            tier = _ssm.ParameterTier.STANDARD
        )

        cityparameter = _ssm.StringParameter(
            self, 'cityparameter',
            parameter_name = '/maxmind/geolite2/city',
            string_value = 'EMPTY',
            description = 'MaxMind GeoLite2 City Last Updated',
            tier = _ssm.ParameterTier.STANDARD
        )

    ### S3 BUCKETS ###

        bucket = _s3.Bucket.from_bucket_name(
            self, 'bucket',
            bucket_name = 'packages-use2-lukach-io'
        )

        use1 = _s3.Bucket.from_bucket_name(
            self, 'use1',
            bucket_name = 'geolite-staged-use1-lukach-io'
        )

        usw2 = _s3.Bucket.from_bucket_name(
            self, 'usw2',
            bucket_name = 'geolite-staged-usw2-lukach-io'
        )

        staged = _s3.Bucket(
            self, 'staged',
            bucket_name = 'geolite-staged-lukach-io',
            encryption = _s3.BucketEncryption.S3_MANAGED,
            block_public_access = _s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy = RemovalPolicy.DESTROY,
            auto_delete_objects = True,
            enforce_ssl = True,
            versioned = False
        )

        deployment = _deployment.BucketDeployment(
            self, 'DeployFunctionFile',
            sources = [_deployment.Source.asset('code')],
            destination_bucket = staged,
            prune = False
        )

        bucket_policy_one = _iam.PolicyStatement(
            effect = _iam.Effect(
                'ALLOW'
            ),
            principals = [
                _iam.AnyPrincipal()
            ],
            actions = [
                's3:ListBucket'
            ],
            resources = [
                staged.bucket_arn
            ],
            conditions = {"StringEquals": {"aws:PrincipalOrgID": organization.string_value}}
        )

        staged.add_to_resource_policy(bucket_policy_one)

        object_policy_one = _iam.PolicyStatement(
            effect = _iam.Effect(
                'ALLOW'
            ),
            principals = [
                _iam.AnyPrincipal()
            ],
            actions = [
                's3:GetObject'
            ],
            resources = [
                staged.arn_for_objects('*')
            ],
            conditions = {"StringEquals": {"aws:PrincipalOrgID": organization.string_value}}
        )

        staged.add_to_resource_policy(object_policy_one)

        research = _s3.Bucket(
            self, 'research',
            bucket_name = 'geolite-research-lukach-io',
            encryption = _s3.BucketEncryption.S3_MANAGED,
            block_public_access = _s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy = RemovalPolicy.DESTROY,
            auto_delete_objects = False,
            enforce_ssl = True,
            versioned = False
        )

        bucket_policy_two = _iam.PolicyStatement(
            effect = _iam.Effect(
                'ALLOW'
            ),
            principals = [
                _iam.AnyPrincipal()
            ],
            actions = [
                's3:ListBucket'
            ],
            resources = [
                research.bucket_arn
            ],
            conditions = {"StringEquals": {"aws:PrincipalOrgID": organization.string_value}}
        )

        research.add_to_resource_policy(bucket_policy_two)

        object_policy_two = _iam.PolicyStatement(
            effect = _iam.Effect(
                'ALLOW'
            ),
            principals = [
                _iam.AnyPrincipal()
            ],
            actions = [
                's3:GetObject'
            ],
            resources = [
                research.arn_for_objects('*')
            ],
            conditions = {"StringEquals": {"aws:PrincipalOrgID": organization.string_value}}
        )

        research.add_to_resource_policy(object_policy_two)

    ### LAMBDA LAYER ###

        requests = _lambda.LayerVersion(
            self, 'requests',
            layer_version_name = 'requests',
            description = str(year)+'-'+str(month)+'-'+str(day)+' deployment',
            code = _lambda.Code.from_bucket(
                bucket = bucket,
                key = 'requests.zip'
            ),
            compatible_architectures = [
                _lambda.Architecture.ARM_64
            ],
            compatible_runtimes = [
                _lambda.Runtime.PYTHON_3_13
            ],
            removal_policy = RemovalPolicy.DESTROY
        )

    ### SECRET MANAGER ###

        secret = _secrets.Secret(
            self, 'secret',
            secret_name = 'geolite',
            secret_object_value = {
                "api": SecretValue.unsafe_plain_text("<EMPTY>"),
                "key": SecretValue.unsafe_plain_text("<EMPTY>")
            }
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
                    'lambda:UpdateFunctionCode',
                    's3:GetObject',
                    's3:PutObject',
                    'ssm:GetParameter',
                    'ssm:PutParameter'
                ],
                resources = [
                    '*'
                ]
            )
        )

        secret.grant_read(role)

    ### LAMBDA FUNCTION ###

        download = _lambda.Function(
            self, 'download',
            runtime = _lambda.Runtime.PYTHON_3_13,
            architecture = _lambda.Architecture.ARM_64,
            code = _lambda.Code.from_asset('download'),
            handler = 'download.handler',
            environment = dict(
                S3_RESEARCH = research.bucket_name,
                S3_STAGED = staged.bucket_name,
                S3_USE1 = use1.bucket_name,
                S3_USW2 = usw2.bucket_name,
                SECRET_MGR_ARN = secret.secret_arn,
                SSM_PARAMETER_ASN = '/maxmind/geolite2/asn',
                SSM_PARAMETER_CITY = '/maxmind/geolite2/city',
                LAMBDA_FUNCTION_USE1 = 'arn:aws:lambda:us-east-1:'+str(account)+':function:search',
                LAMBDA_FUNCTION_USW2 = 'arn:aws:lambda:us-west-2:'+str(account)+':function:search'
            ),
            ephemeral_storage_size = Size.gibibytes(1),
            timeout = Duration.seconds(900),
            memory_size = 2048,
            role = role,
            layers = [
                requests
            ]
        )

        logs = _logs.LogGroup(
            self, 'logs',
            log_group_name = '/aws/lambda/'+download.function_name,
            retention = _logs.RetentionDays.ONE_WEEK,
            removal_policy = RemovalPolicy.DESTROY
        )

        event = _events.Rule(
            self, 'event',
            schedule = _events.Schedule.cron(
                minute = '0',
                hour = '*',
                month = '*',
                week_day = '*',
                year = '*'
            )
        )

        event.add_target(
            _targets.LambdaFunction(
                download
            )
        )

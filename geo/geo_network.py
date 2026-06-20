from aws_cdk import Stack
from aws_cdk import aws_ec2 as _ec2
from aws_cdk import aws_elasticache as _elasticache
from constructs import Construct


class GeoNetworkStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = _ec2.Vpc(
            self,
            "geovpc",
            ip_addresses=_ec2.IpAddresses.cidr("10.255.255.0/24"),
            max_azs=3,
            nat_gateways=0,
            subnet_configuration=[
                _ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=_ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=26,
                ),
            ],
        )

        self.vpc.add_gateway_endpoint(
            "s3gatewayendpoint",
            service=_ec2.GatewayVpcEndpointAwsService.S3,
        )
        self.vpc.add_gateway_endpoint(
            "dynamodbgatewayendpoint",
            service=_ec2.GatewayVpcEndpointAwsService.DYNAMODB,
        )

        self.vpc.add_interface_endpoint(
            "sqsinterfaceendpoint",
            service=_ec2.InterfaceVpcEndpointAwsService.SQS,
            subnets=_ec2.SubnetSelection(subnet_type=_ec2.SubnetType.PRIVATE_ISOLATED),
        )

        self.process_security_group = _ec2.SecurityGroup(
            self,
            "processsecuritygroup",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="Security group for geolite2-process lambda",
        )

        valkey_security_group = _ec2.SecurityGroup(
            self,
            "valkeysecuritygroup",
            vpc=self.vpc,
            allow_all_outbound=True,
            description="Security group for Valkey serverless cache",
        )
        valkey_security_group.add_ingress_rule(
            self.process_security_group,
            _ec2.Port.tcp(6379),
            "Allow process lambda to access Valkey",
        )

        self.valkey_cache = _elasticache.CfnServerlessCache(
            self,
            "valkeyserverlesscache",
            engine="valkey",
            serverless_cache_name="geo-valkey-serverless-v2",
            subnet_ids=[subnet.subnet_id for subnet in self.vpc.isolated_subnets],
            security_group_ids=[valkey_security_group.security_group_id],
            description="Geo IP range cache for process/search lambdas",
        )

        self.valkey_endpoint = self.valkey_cache.attr_endpoint_address
        self.valkey_port = self.valkey_cache.attr_endpoint_port

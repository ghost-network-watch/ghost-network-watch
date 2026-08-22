"""Ghost Network Watch infrastructure (AWS CDK, Python).

Two stacks, deployable independently:

  GnwSiteStack      S3 + CloudFront + ACM for ghostnetworkwatch.org.
                    Deploy first; DNS validation records go into Porkbun.
  GnwPipelineStack  Monthly Fargate task on an EventBridge schedule that runs
                    ops/run_monthly.sh: crawl, parse, refs, compact, flags,
                    score, site build, notify bundles, site deploy. Failure
                    alerts by email. Notifications to insurers are generated
                    as files, never sent automatically.

Deploy (from infra/):
  pip install -r requirements.txt
  cdk bootstrap                       # once per account and region
  cdk deploy GnwSiteStack             # then add the DNS records it outputs
  cdk deploy GnwPipelineStack -c alertEmail=you@example.com

Kill switch: disable the EventBridge rule (GnwPipelineStack/MonthlySchedule)
or set -c scheduleEnabled=false and redeploy.
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

DOMAIN = "ghostnetworkwatch.org"


class GnwSiteStack(Stack):
    """Static site hosting: private S3 origin behind CloudFront."""

    def __init__(self, scope: Construct, cid: str, **kwargs) -> None:
        super().__init__(scope, cid, **kwargs)

        self.site_bucket = s3.Bucket(
            self, "SiteBucket",
            bucket_name=f"gnw-site-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )

        cert = acm.Certificate(
            self, "SiteCert",
            domain_name=DOMAIN,
            subject_alternative_names=[f"www.{DOMAIN}"],
            validation=acm.CertificateValidation.from_dns(),
        )

        # S3 REST origins do not resolve directory indexes; rewrite /x/ to
        # /x/index.html at the edge.
        index_rewrite = cloudfront.Function(
            self, "IndexRewrite",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {"
                "  var req = event.request;"
                "  if (req.uri.endsWith('/')) { req.uri += 'index.html'; }"
                "  else if (!req.uri.includes('.')) { req.uri += '/index.html'; }"
                "  return req;"
                "}"
            ),
        )

        self.distribution = cloudfront.Distribution(
            self, "SiteDistribution",
            comment="ghostnetworkwatch.org static site",
            default_root_object="index.html",
            domain_names=[DOMAIN, f"www.{DOMAIN}"],
            certificate=cert,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.site_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=index_rewrite,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=404,
                    response_page_path="/404.html",
                    ttl=Duration.minutes(5),
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=404,
                    response_page_path="/404.html",
                    ttl=Duration.minutes(5),
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

        cdk.CfnOutput(self, "SiteBucketName", value=self.site_bucket.bucket_name)
        cdk.CfnOutput(self, "DistributionId", value=self.distribution.distribution_id)
        cdk.CfnOutput(
            self, "DistributionDomain",
            value=self.distribution.distribution_domain_name,
            description="Point Porkbun ALIAS (apex) and CNAME (www) here",
        )


class GnwPipelineStack(Stack):
    """Monthly pipeline: EventBridge schedule -> Fargate task -> S3 + site."""

    def __init__(
        self, scope: Construct, cid: str,
        site_bucket: s3.IBucket, distribution: cloudfront.IDistribution,
        **kwargs,
    ) -> None:
        super().__init__(scope, cid, **kwargs)

        alert_email = self.node.try_get_context("alertEmail") or "contact@ghostnetworkwatch.org"
        schedule_enabled = (self.node.try_get_context("scheduleEnabled") or "true") == "true"
        # Until launch day the scheduled run builds only the prelaunch site,
        # so findings cannot publish before insurer notification completes.
        prelaunch = (self.node.try_get_context("prelaunch") or "true") == "true"

        data_bucket = s3.Bucket(
            self, "DataBucket",
            bucket_name=f"gnw-data-{self.account}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="blobs-to-glacier",
                    prefix="blobs/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
                            transition_after=Duration.days(90),
                        )
                    ],
                )
            ],
        )

        # Public subnets with public IPs for crawler egress; no NAT gateway
        # (the task holds no inbound ports and no secrets beyond its role).
        vpc = ec2.Vpc(
            self, "PipelineVpc",
            max_azs=2, nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                )
            ],
        )
        cluster = ecs.Cluster(self, "PipelineCluster", vpc=vpc)

        image = ecr_assets.DockerImageAsset(
            self, "PipelineImage",
            directory="..",
            file="Dockerfile",
            platform=ecr_assets.Platform.LINUX_ARM64,
        )

        task_def = ecs.FargateTaskDefinition(
            self, "MonthlyTask",
            cpu=4096,
            memory_limit_mib=16384,
            ephemeral_storage_gib=200,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_def.add_container(
            "pipeline",
            image=ecs.ContainerImage.from_docker_image_asset(image),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="gnw",
                log_retention=logs.RetentionDays.SIX_MONTHS,
            ),
            environment={
                "GNW_DATA_BUCKET": data_bucket.bucket_name,
                "GNW_SITE_BUCKET": site_bucket.bucket_name,
                "GNW_DISTRIBUTION_ID": distribution.distribution_id,
                "GNW_PYTHON": "python3",
                **({"GNW_PRELAUNCH": "1"} if prelaunch else {}),
            },
        )
        data_bucket.grant_read_write(task_def.task_role)
        site_bucket.grant_read_write(task_def.task_role)
        task_def.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[
                    f"arn:aws:cloudfront::{self.account}:distribution/"
                    f"{distribution.distribution_id}"
                ],
            )
        )

        # The 15th, 12:00 UTC: after the NPPES monthly release (around the
        # 10th) and the CMS PUF refresh cadence.
        rule = events.Rule(
            self, "MonthlySchedule",
            enabled=schedule_enabled,
            schedule=events.Schedule.cron(minute="0", hour="12", day="15", month="*", year="*"),
            description="Ghost Network Watch monthly pipeline run",
        )
        rule.add_target(
            targets.EcsTask(
                cluster=cluster,
                task_definition=task_def,
                subnet_selection=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
                assign_public_ip=True,
                retry_attempts=1,
            )
        )

        alerts = sns.Topic(self, "PipelineAlerts", display_name="GNW pipeline alerts")
        alerts.add_subscription(subs.EmailSubscription(alert_email))
        events.Rule(
            self, "TaskFailureAlert",
            description="Alert when the monthly pipeline task exits nonzero",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [cluster.cluster_arn],
                    "lastStatus": ["STOPPED"],
                    "containers": {"exitCode": [{"anything-but": 0}]},
                },
            ),
            targets=[targets.SnsTopic(alerts)],
        )

        cdk.CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)
        cdk.CfnOutput(
            self, "ManualRunCommand",
            value=(
                f"aws ecs run-task --cluster {cluster.cluster_name} "
                "--launch-type FARGATE --task-definition "
                f"{task_def.family} --network-configuration "
                "'awsvpcConfiguration={subnets=[SUBNET_ID],assignPublicIp=ENABLED}'"
            ),
            description="Run the pipeline outside the schedule",
        )


app = cdk.App()
env = cdk.Environment(region="us-east-1")  # ACM for CloudFront requires us-east-1
site = GnwSiteStack(app, "GnwSiteStack", env=env)
GnwPipelineStack(
    app, "GnwPipelineStack",
    site_bucket=site.site_bucket, distribution=site.distribution, env=env,
)
app.synth()

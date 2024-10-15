from aws_cdk import (
    # Duration,
    aws_ecs as ecs,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_logs as logs,
    aws_s3 as s3,
    aws_iam as iam,
    aws_autoscaling as autoscaling,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_actions as elb_actions,
    aws_elasticloadbalancingv2_targets as targets,
    aws_events as events,
    aws_events_targets as event_targets,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    Duration,
    RemovalPolicy,
    CustomResource,
    # aws_sqs as sqs,
    CfnOutput,
    aws_efs as efs
)
from constructs import Construct
import json, hashlib
from cdk_nag import NagSuppressions

# with open(
#     "./cdk_comfyui/cert.json",
#     "r",
# ) as file:
#     config = json.load(file)

class CdkComfyuiStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Setting
        unique_input = f"{self.account}-{self.region}-comfyui"
        unique_hash = hashlib.sha256(unique_input.encode('utf-8')).hexdigest()[:10]
        suffix = unique_hash.lower()
        
        # Get context
        autoScaleDown = self.node.try_get_context("autoScaleDown")
        if autoScaleDown is None:
            autoScaleDown = True

        cheapVpc = self.node.try_get_context("cheapVpc") or False
        
        scheduleAutoScaling = self.node.try_get_context("scheduleAutoScaling") or True
        timezone = self.node.try_get_context("timezone") or "UTC"
        scheduleScaleUp = self.node.try_get_context("scheduleScaleUp") or "0 2 * * 1-5"
        scheduleScaleDown = self.node.try_get_context("scheduleScaleDown") or "0 14 * * 1-5"
        
        keyPair=ec2.KeyPair.from_key_pair_attributes(
                self,
                id='KeyPair', 
                key_pair_name='YourKeyPairName')

        if cheapVpc:
            natInstance = ec2.NatProvider.instance_v2(
                instance_type=ec2.InstanceType("t4g.nano"),
                default_allowed_traffic=ec2.NatTrafficDirection.OUTBOUND_ONLY,
                key_pair=keyPair,
            )

        vpc = ec2.Vpc(
            self, "ComfyVPC",
            max_azs=2,  # Define the maximum number of Availability Zones
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                )
            ],
            nat_gateway_provider=natInstance if cheapVpc else None,
            gateway_endpoints={
                # ECR Image Layer
                "S3": ec2.GatewayVpcEndpointOptions(
                    service=ec2.GatewayVpcEndpointAwsService.S3
                )
            }
        )
        

        if cheapVpc:
            natInstance.security_group.add_ingress_rule(
                ec2.Peer.ipv4(vpc.vpc_cidr_block),
                ec2.Port.all_traffic(),
                "Allow NAT Traffic from inside VPC",
            )
            
        # Create ALB Security Group
        alb_security_group = ec2.SecurityGroup(
            self,
            "ALBSecurityGroup",
            vpc=vpc,
            description="Security Group for ALB",
            allow_all_outbound=True,
        )

        alb_security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "Allow HTTP traffic on target IP",
        )
        
        # Create Auto Scaling Group Security Group
        asg_security_group = ec2.SecurityGroup(
            self,
            "AsgSecurityGroup",
            vpc=vpc,
            description="Security Group for ASG",
            allow_all_outbound=True,
        )

        # Allow NFS access within the EFS security group
        asg_security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(2049),
            description="Allow NFS access from within the EFS security group"
        )

        # EC2 Role for AWS internal use (if necessary)
        ec2_role = iam.Role(
            self,
            "EC2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2FullAccess"), # check if less privilege can be given
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedEC2InstanceDefaultPolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonElasticFileSystemFullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonElasticFileSystemClientFullAccess")
                # iam.ManagedPolicy.from_aws_managed_policy_name("AmazonEC2ContainerServiceforEC2Role")
            ]
        )

        efs_sg = ec2.SecurityGroup(
            self, "EFSSG",
            vpc=vpc,
            description="Allow NFS access to EFS",
            allow_all_outbound=True
        )
        
        # Allow NFS access within the EFS security group
        efs_sg.add_ingress_rule(
            peer= ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(2049),
            description="Allow NFS access from within the EFS security group"
        )

         # Create EFS File System
        file_system = efs.FileSystem(
            self, "EFS",
            vpc=vpc,
            security_group=efs_sg,
            #lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,  # Files moved to IA after 14 days
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            removal_policy=RemovalPolicy.DESTROY,  # NOT recommended for production
            # encrypted=True  # Enable encryption if needed
        )

        # Optionally, create an Access Point
        access_point = file_system.add_access_point(
            "AccessPoint",
            path="/home", # "/home/user/opt/ComfyUI",
            create_acl=efs.Acl(
                owner_uid="0",
                owner_gid="0",
                permissions="755"
            ),
            posix_user=efs.PosixUser(
                gid="0",
                uid="0"
            )
        )
        

        # Create an Auto Scaling Group with two EBS volumes
        launchTemplate = ec2.LaunchTemplate(
            self,
            "Host",
            launch_template_name="ComfyLaunchTemplateHost",
            instance_type=ec2.InstanceType("g6.2xlarge"),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(
                hardware_type=ecs.AmiHardwareType.GPU
            ),
            role=ec2_role,
            security_group=asg_security_group,
            key_pair=keyPair,
            # associate_public_ip_address=True,
            # user_data=user_data_script,
            block_devices=[
                ec2.BlockDevice(
                    device_name="/dev/xvda",
                    volume=ec2.BlockDeviceVolume.ebs(volume_size=100,
                                                     encrypted=True)
                )
            ],
        )

        auto_scaling_group = autoscaling.AutoScalingGroup(
            self,
            "ASG",
            auto_scaling_group_name="ComfyASG",
            vpc=vpc,
            mixed_instances_policy=autoscaling.MixedInstancesPolicy(
                instances_distribution=autoscaling.InstancesDistribution(
                    on_demand_percentage_above_base_capacity=100,
                    on_demand_allocation_strategy=autoscaling.OnDemandAllocationStrategy.LOWEST_PRICE,
                ),
                launch_template=launchTemplate,
                launch_template_overrides=[
                    autoscaling.LaunchTemplateOverrides(instance_type=ec2.InstanceType("g6.2xlarge")),
                    autoscaling.LaunchTemplateOverrides(instance_type=ec2.InstanceType("g5.2xlarge")),
                    # autoscaling.LaunchTemplateOverrides(instance_type=ec2.InstanceType("g4dn.2xlarge")),
                ],
            ),
            min_capacity=0,
            max_capacity=1,
            desired_capacity=1,
            new_instances_protected_from_scale_in=False,
        )
        
        auto_scaling_group.apply_removal_policy(RemovalPolicy.DESTROY)

        cpu_utilization_metric = cloudwatch.Metric(
            namespace='AWS/EC2',
            metric_name='CPUUtilization',
            dimensions_map={
                'AutoScalingGroupName': auto_scaling_group.auto_scaling_group_name
            },
            statistic='Average',
            period=Duration.minutes(1)
        )

        if scheduleAutoScaling:
            # Create a scheduled action to set the desired capacity to 0
            after_work_hours_action = autoscaling.ScheduledAction(
                self,
                "AfterWorkHoursAction",
                auto_scaling_group=auto_scaling_group,
                desired_capacity=0,
                time_zone=timezone,
                schedule=autoscaling.Schedule.expression(scheduleScaleDown)
            )
            # Create a scheduled action to set the desired capacity to 1
            start_work_hours_action = autoscaling.ScheduledAction(
                self,
                "StartWorkHoursAction",
                auto_scaling_group=auto_scaling_group,
                desired_capacity=1,
                time_zone=timezone,
                schedule=autoscaling.Schedule.expression(scheduleScaleUp)
            )
            
        # Create an ECS Cluster
        cluster = ecs.Cluster(
            self, "ComfyUICluster", 
            vpc=vpc, 
            cluster_name="ComfyUICluster", 
            container_insights=True
        )
        
        # Create ASG Capacity Provider for the ECS Cluster
        capacity_provider = ecs.AsgCapacityProvider(
            self, "AsgCapacityProvider",
            auto_scaling_group=auto_scaling_group,
            enable_managed_scaling=False,  # Enable managed scaling
            enable_managed_termination_protection=False,  # Disable managed termination protection
            target_capacity_percent=100
        )
        
        cluster.add_asg_capacity_provider(capacity_provider)
        
        # Create IAM Role for ECS Task Execution
        task_exec_role = iam.Role(
            self,
            "ECSTaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonElasticFileSystemClientFullAccess"),
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonElasticFileSystemFullAccess")
            ],
        )

        
        # ECR Repository
        ecr_repository = ecr.Repository.from_repository_name(
            self, 
            "comfyui-ecs", 
            "comfyui-ecs")
            

        # CloudWatch Logs Group
        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name="/ecs/comfy-ecs-ui",
            removal_policy=RemovalPolicy.DESTROY,
        )


        task_definition = ecs.Ec2TaskDefinition(
            self,
            "TaskDef",
            network_mode=ecs.NetworkMode.AWS_VPC,
            task_role=task_exec_role,
            execution_role=task_exec_role,
            # volumes=[volume]
        )

        task_definition.add_volume(
            name="ComfyUIVolume",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=file_system.file_system_id,
                root_directory="/",
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=access_point.access_point_id,
                    iam="ENABLED"
                ),
                # transit_encryption_port=2049
            )
        )

        # file_system.grant_root_access(task_exec_role.grant_principal)

        # Add container to the task definition
        container = task_definition.add_container(
            "ComfyUIContainer",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repository, "latest"),
            gpu_count=1,
            memory_reservation_mib=30720,
            cpu=7680,
            logging=ecs.LogDriver.aws_logs(stream_prefix="comfy-ecs-ui", log_group=log_group),
            # health_check=ecs.HealthCheck(
            #     command=["CMD-SHELL", "curl -f http://localhost:8181/system_stats || exit 1"],
            #     interval=Duration.seconds(30),
            #     timeout=Duration.seconds(10),
            #     retries=9,
            #     start_period=Duration.seconds(200)
            # )
        )
        
        container.add_mount_points(
            ecs.MountPoint(
                container_path="/models", # Note: this path must be mapping to the folders under the comfyUI. WTF
                source_volume="ComfyUIVolume",
                read_only=False
            )
        )

        # Port mappings for the container
        container.add_port_mappings(
            ecs.PortMapping(
                container_port=8181,
                host_port=8181,
                app_protocol=ecs.AppProtocol.http,
                name="comfyui-port-mapping",
                protocol=ecs.Protocol.TCP,
            )
        )
        
        # Create ECS Service Security Group
        service_security_group = ec2.SecurityGroup(
            self,
            "ServiceSecurityGroup",
            vpc=vpc,
            description="Security Group for ECS Service",
            allow_all_outbound=True,
        )

        # Allow inbound traffic on port 8181
        service_security_group.add_ingress_rule(
            ec2.Peer.security_group_id(alb_security_group.security_group_id),
            ec2.Port.tcp(8181),
            "Allow inbound traffic on port 8181",
        )

        # 
        efs_sg.add_ingress_rule(
            peer= ec2.Peer.security_group_id(service_security_group.security_group_id),
            connection=ec2.Port.tcp(2049),
            description="Allow NFS access from within the EFS security group"
        )
        
        # Create ECS Service
        service = ecs.Ec2Service(
            self,
            "ComfyUIService",
            service_name="ComfyUIService",
            cluster=cluster,
            task_definition=task_definition,
            capacity_provider_strategies=[
                ecs.CapacityProviderStrategy(
                    capacity_provider=capacity_provider.capacity_provider_name, weight=1
                )
            ],
            security_groups=[service_security_group],
            health_check_grace_period=Duration.seconds(120),
            min_healthy_percent=0,
            enable_execute_command=True
        )

        # Application Load Balancer
        alb = elbv2.ApplicationLoadBalancer(
            self, "ComfyUIALB",
            vpc=vpc,
            load_balancer_name="ComfyUIALB",
            internet_facing=True,
            security_group=alb_security_group
        )
        
        
        # Add target groups for ECS service
        ecs_target_group = elbv2.ApplicationTargetGroup(
            self,
            "EcsTargetGroup",
            port=8181,
            vpc=vpc,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            targets=[
                service.load_balancer_target(
                    container_name="ComfyUIContainer", container_port=8181
                )],
            health_check=elbv2.HealthCheck(
                enabled=True,
                path="/system_stats",
                port="8181",
                protocol=elbv2.Protocol.HTTP,
                healthy_http_codes="200",  # Adjust as needed
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                unhealthy_threshold_count=5,
                healthy_threshold_count=3,
            )
        )
        
        
        # Add listener to the Load Balancer on port 443
        listener = alb.add_listener(
            "Listener", 
            port=80, 
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_action=elbv2.ListenerAction.forward([ecs_target_group])
        )
        
        
        NagSuppressions.add_resource_suppressions(
            [alb_security_group,asg_security_group,service_security_group,alb],
            suppressions=[
                {"id": "AwsSolutions-EC23",
                 "reason": "The Security Group and ALB needs to allow 0.0.0.0/0 inbound access for the ALB to be publicly accessible. Additional security is provided via Cognito authentication."
                },
                { "id": "AwsSolutions-ELB2",
                 "reason": "Adding access logs requires extra S3 bucket so removing it for sample purposes."},
            ],
            apply_to_children=True
        )
        
        NagSuppressions.add_resource_suppressions(
            [task_definition],
            suppressions=[
                {"id": "AwsSolutions-ECS2",
                 "reason": "Recent aws-cdk-lib version adds 'AWS_REGION' environment variable implicitly."
                },
            ],
            apply_to_children=True
        )
        NagSuppressions.add_resource_suppressions(
            [vpc],
            suppressions=[
                {"id": "AwsSolutions-EC28",
                "reason": "NAT Instance does not require autoscaling."
                },
                {"id": "AwsSolutions-EC29",
                "reason": "NAT Instance does not require autoscaling."
                },
            ],
            apply_to_children=True
        )

        CfnOutput(self, "alb address", value=alb.load_balancer_dns_name)
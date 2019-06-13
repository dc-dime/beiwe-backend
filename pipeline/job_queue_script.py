"""
A script for creating a setup to run AWS Batch jobs: a compute environment, a job queue and a
job definition to use as a template for actual jobs.
"""
from __future__ import print_function

import json
import os.path
from time import sleep

import boto3

from script_helpers import set_default_region
from configuration_getters import get_aws_object_names, get_configs_folder, get_current_region


def run(repo_uri, ami_id):
    """
    Run the code
    :param repo_uri: string, the URI of an existing AWS ECR repository.
    :param ami_id: string, the id of an existing AWS AMI.
    """
    
    # Load a bunch of JSON blobs containing policies and other things that boto3 clients
    # require as input.
    configs_folder = get_configs_folder()
    
    with open(os.path.join(configs_folder, 'assume-batch-role.json')) as fn:
        assume_batch_role_policy_json = json.dumps(json.load(fn))
    with open(os.path.join(configs_folder, 'batch-service-role.json')) as fn:
        batch_service_role_policy_json = json.dumps(json.load(fn))
    with open(os.path.join(configs_folder, 'assume-ec2-role.json')) as fn:
        assume_ec2_role_policy_json = json.dumps(json.load(fn))
    with open(os.path.join(configs_folder, 'batch-instance-role.json')) as fn:
        batch_instance_role_policy_json = json.dumps(json.load(fn))
    with open(os.path.join(configs_folder, 'compute-environment.json')) as fn:
        compute_environment_dict = json.load(fn)
    with open(os.path.join(configs_folder, 'container-props.json')) as fn:
        container_props_dict = json.load(fn)
    aws_object_names = get_aws_object_names()
    print('JSON loaded')
    
    # Grab the names from aws_object_names
    comp_env_role = aws_object_names['comp_env_role']
    comp_env_name = aws_object_names['comp_env_name']
    instance_profile = aws_object_names['instance_profile']
    security_group = aws_object_names['security_group']
    
    if "subnets" not in compute_environment_dict:
        # "subnets": ["subnet-af1f02e6"]
        ec2_client = boto3.client('ec2')
        subnets = ec2_client.describe_subnets()['Subnets']
        if len(set([y['VpcId'] for y in subnets])) != 1:
            print("\n")
            print("It looks like you have multiple VPCs in this region, which means this script")
            print("cannot automatically determine the correct subnets on which to place")
            print("the data pipeline compute servers.")
            print("You can resolve this by adding a line with the key 'subnets' like the following")
            print("to the compute-environment.json file in the configs folder.")
            print("""  "subnets": ["subnet-abc123"]""")
            exit(1)
        else:
            # add a 1 item list containing a valid subnet
            compute_environment_dict['subnets'] = [subnets[0]['SubnetId']]
    
    # Create a new IAM role for the compute environment
    set_default_region()
    iam_client = boto3.client('iam')

    try:
        comp_env_role_arn = iam_client.create_role(
            RoleName=comp_env_role,
            AssumeRolePolicyDocument=assume_batch_role_policy_json,
        )['Role']['Arn']
    except Exception as e:
        if "Role with name AWSBatchServiceRole already exists." in str(e):
            comp_env_role_arn = iam_client.get_role(RoleName=comp_env_role)['Role']['Arn']
        else:
            raise

    try:
        iam_client.put_role_policy(
            RoleName=comp_env_role,
            PolicyName='aws-batch-service-policy',  # This name isn't used anywhere else
            PolicyDocument=batch_service_role_policy_json,
        )
        print('Batch role created')
    except Exception:
        print('WARNING: Batch service role creation failed, assuming that this means it already exists.')
    
    # Create an EC2 instance profile for the compute environment
    try:
        iam_client.create_role(
            RoleName=instance_profile,
            AssumeRolePolicyDocument=assume_ec2_role_policy_json,
        )
    except Exception:
        print('WARNING: Batch role creation failed, assuming that this means it already exists.')

    try:
        iam_client.put_role_policy(
            RoleName=instance_profile,
            PolicyName='aws-batch-instance-policy',  # This name isn't used anywhere else
            PolicyDocument=batch_instance_role_policy_json,
        )
    except Exception:
        print('WARNING: assigning role creation failed, assuming that this means it already exists.')


    try:
        resp = iam_client.create_instance_profile(InstanceProfileName=instance_profile)
    except Exception as e:
        if "Instance Profile ecsInstanceRole already exists." in str(e):
            resp = iam_client.get_instance_profile(InstanceProfileName=instance_profile)

    compute_environment_dict['instanceRole'] = resp['InstanceProfile']['Arn']
    try:
        iam_client.add_role_to_instance_profile(
            InstanceProfileName=instance_profile,
            RoleName=instance_profile,
        )
        print('Instance profile created')
    except Exception as e:
        if not "Cannot exceed quota for InstanceSessionsPerInstanceProfile" in str(e):
            raise
    
    # Create a security group for the compute environment
    ec2_client = boto3.client('ec2')

    try:
        group_id = ec2_client.describe_security_groups(GroupNames=[security_group])['SecurityGroups'][0]['GroupId']
    except Exception:
        try:
            group_id = ec2_client.create_security_group(
                Description='Security group for AWS Batch',
                GroupName=security_group,
            )['GroupId']
        except Exception as e:
            if "InvalidGroup.Duplicate" not in str(e):
                raise

    # the raise condition above is sufficient for this potential unbound local error
    compute_environment_dict['securityGroupIds'] = [group_id]
    
    # Create the batch compute environment
    batch_client = boto3.client('batch')
    compute_environment_dict['imageId'] = ami_id


    try:
        batch_client.create_compute_environment(
            computeEnvironmentName=comp_env_name,
            type='MANAGED',
            computeResources=compute_environment_dict,
            serviceRole=comp_env_role_arn,
        )
    except Exception:
        print('WARNING: creating compute environment failed, this probably means it already exists.')

    # The compute environment takes somewhere between 10 and 45 seconds to create. Until it
    # is created, we cannot create a job queue. So first, we wait until the compute environment
    # has finished being created.
    print('Waiting for compute environment...')
    while True:
        # Ping the AWS server for a description of the compute environment
        resp = batch_client.describe_compute_environments(
            computeEnvironments=[comp_env_name],
        )
        status = resp['computeEnvironments'][0]['status']
        
        if status == 'VALID':
            # If the compute environment is valid, we can proceed to creating the job queue
            break
        elif status == 'CREATING' or status == 'UPDATING':
            # If the compute environment is still being created (or has been created and is
            # now being modified), we wait one second and then ping the server again.
            sleep(1)
        else:
            # If the compute environment is invalid (or deleting or deleted), we cannot
            # continue with job queue creation. Raise an error and quit the script.
            raise RuntimeError('Compute Environment is Invalid')
    print('Compute environment created')


    # Create a batch job queue
    try:
        batch_client.create_job_queue(
            jobQueueName=aws_object_names['queue_name'],
            priority=1,
            computeEnvironmentOrder=[{'order': 0, 'computeEnvironment': comp_env_name}],
        )
        print('Job queue created')
    except Exception:
        print("Warning: job queue '%s' already exists." % aws_object_names['queue_name'])

    # Create a batch job definition
    container_props_dict['image'] = repo_uri
    container_props_dict['environment'] = [
        {
            'name': 'access_key_ssm_name',
            'value': aws_object_names['access_key_ssm_name'],
        }, {
            'name': 'secret_key_ssm_name',
            'value': aws_object_names['secret_key_ssm_name'],
        }, {
            'name': 'region_name',
            'value': get_current_region(),
        }, {
            'name': 'server_url',
            'value': aws_object_names['server_url'],
        },
    ]

    base_name = aws_object_names['job_defn_name']
    job_definition_name = base_name
    for i in range(100):

        # do not give up at this point, try to create the
        if i != 1:
            old_name = job_definition_name
            job_definition_name = "%s_%s" % (base_name, i)
            print(
                "Warning: job definition '%s' already exists.  Attempting to create a job"
                " definition named '%s'.  You will need to change your Beiwe server settings "
                " job_defn_name to match this name."
                % (old_name, job_definition_name)
            )

        try:
            batch_client.register_job_definition(
                jobDefinitionName=job_definition_name,
                type='container',
                containerProperties=container_props_dict,
            )
            print('Job definition "%s" created.' % job_definition_name)
            break
        except Exception:
            print('Job definition "%s" NOT created.' % job_definition_name)

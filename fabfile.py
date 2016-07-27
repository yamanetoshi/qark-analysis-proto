#!/usr/bin/python
# coding: utf-8

# Copyright 2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file.
# This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations under the License.

#
# fabfile.py
#

#
# A Python fabric file for setting up and managing the AWS ECS POV-Ray worker demo.
# This file assumes python2 is installed on the system as the default.
#

# Imports
from fabric.api import local, quiet, env, run, put, lcd, cd
from ConfigParser import ConfigParser
import boto
import boto.s3
from boto.exception import BotoServerError
from zipfile import ZipFile, ZIP_DEFLATED
import os
import json
import time
from urllib2 import unquote
from cStringIO import StringIO
import shutil

# Constants (User configurable), imported from config.py

from config import *

# Constants (Application specific)
BUCKET_POSTFIX = '-android-test-bucket'  # Gets put after the unix user ID to create the bucket name.
SSH_KEY_DIR = os.environ['HOME'] + '/.ssh'
SQS_QUEUE_NAME = APP_NAME + 'Queue'
LAMBDA_FUNCTION_NAME = 'ecs-worker-launcher'
LAMBDA_FUNCTION_DEPENDENCIES = 'async'
ECS_TASK_NAME = APP_NAME + 'Task'

# Constants (OS specific)
USER = os.environ['HOME'].split('/')[-1]
AWS_BUCKET = USER + BUCKET_POSTFIX
AWS_CONFIG_FILE_NAME = os.environ['HOME'] + '/.aws/config'
AWS_CREDENTIAL_FILE_NAME = os.environ['HOME'] + '/.aws/credentials'

# Constants
AWS_CLI_STANDARD_OPTIONS = (
    '    --region ' + AWS_REGION +
    '    --profile ' + AWS_PROFILE +
    '    --output json'
)

SSH_USER = 'ubuntu'
CPU_SHARES = 512  # POV-Ray needs at least half a CPU to work nicely.
MEMORY = 512
ZIPFILE_NAME = LAMBDA_FUNCTION_NAME + '.zip'

BUCKET_PERMISSION_SID = APP_NAME + 'Permission'
WAIT_TIME = 5  # seconds to allow for eventual consistency to kick in.
RETRIES = 5  # Number of retries before we give up on something.

# Templates and embedded scripts

LAMBDA_EXECUTION_ROLE_NAME = APP_NAME + '-Lambda-Execution-Role'
LAMBDA_EXECUTION_ROLE_POLICY_NAME = 'AWSLambdaExecutionPolicy'
LAMBDA_EXECUTION_ROLE_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "logs:*",
                "sqs:SendMessage",
                "ec2:StartInstances"
            ],
            "Resource": [
                "arn:aws:logs:*:*:*",
                "arn:aws:sqs:*:*:*",
                "arn:aws:ec2:*:*:*"
            ]
        }
    ]
}

LAMBDA_EXECUTION_ROLE_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "",
            "Effect": "Allow",
            "Principal": {
                "Service": "lambda.amazonaws.com"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}

LAMBDA_FUNCTION_CONFIG = {
    "s3_key_suffix_whitelist": ['.apk', '.zip'],  # Only S3 keys with this URL will be accepted.
    "queue": '',  # To be filled in with the queue ARN.
    "instance_id": EC2_INSTANCE_ID
}

LAMBDA_FUNCTION_CONFIG_PATH = './' + LAMBDA_FUNCTION_NAME + '/config.json'

BUCKET_NOTIFICATION_CONFIGURATION = {
    "LambdaFunctionConfigurations": [
        {
            "Id": APP_NAME,
            "LambdaFunctionArn": "",
            "Events": [
                "s3:ObjectCreated:*"
            ],
            "Filter": {
                "Key": {
                    "FilterRules": [{
                            "Name": "suffix",
                            "Value": "apk"
                    }]
                }
            }
        }
    ]
}

ANON_BUCKET_ACCESS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid":"AddPerm",
            "Effect":"Allow",
            "Principal": "*",
            "Action": ["s3:GetObject"],
            "Resource":""
        }
    ]
}

ECS_ROLE_BUCKET_ACCESS_ROLE_NAME = APP_NAME + '-Bucket-Access-Role'
ECS_ROLE_BUCKET_ACCESS_POLICY_NAME = APP_NAME + "BucketAccessPolicy"
ECS_ROLE_BUCKET_ACCESS_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListAllMyBuckets"
            ],
            "Resource": "arn:aws:s3:::*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:ListBucket",
                "s3:GetBucketLocation"
            ],
            "Resource": ""  # To be filled in by a function below.
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject",
                "s3:GetObject",
                "s3:DeleteObject"
            ],
            "Resource": ""  # To be filled in by a function below.
        }
    ]
}

WORKER_PATH = 'ecs-worker'
WORKER_FILE = 'ecs-worker.sh'


# Functions


# Dependencies and credentials.


def update_dependencies():
    local('pip install -r requirements.txt')
    local('cd ' + LAMBDA_FUNCTION_NAME + '; npm install ' + LAMBDA_FUNCTION_DEPENDENCIES)


def get_aws_credentials():
    config = ConfigParser()
    config.read(AWS_CONFIG_FILE_NAME)
    config.read(AWS_CREDENTIAL_FILE_NAME)
    return config.get(AWS_PROFILE, 'aws_access_key_id'), config.get(AWS_PROFILE, 'aws_secret_access_key')

# AWS IAM


def get_iam_connection():
    aws_access_key_id, aws_secret_access_key = get_aws_credentials()
    return boto.connect_iam(aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key)

# AWS Lambda


def dump_lambda_function_configuration():
    print('Writing config for Lambda function...')
    lambda_function_config = LAMBDA_FUNCTION_CONFIG.copy()
    lambda_function_config['queue'] = get_queue_url()
    with open(LAMBDA_FUNCTION_CONFIG_PATH, 'w') as fp:
        fp.write(json.dumps(lambda_function_config))


def create_lambda_deployment_package():
    print('Creating ZIP file: ' + ZIPFILE_NAME + '...')
    with ZipFile(ZIPFILE_NAME, 'w', ZIP_DEFLATED) as z:
        saved_dir = os.getcwd()
        os.chdir(LAMBDA_FUNCTION_NAME)
        for root, dirs, files in os.walk('.'):
            for basename in files:
                filename = os.path.join(root, basename)
                print('Adding: ' + filename + '...')
                z.write(filename)
        os.chdir(saved_dir)
        z.close()


def get_or_create_lambda_execution_role():
    iam = get_iam_connection()
    target_policy = json.dumps(LAMBDA_EXECUTION_ROLE_TRUST_POLICY, sort_keys=True)

    try:
        result = iam.get_role(LAMBDA_EXECUTION_ROLE_NAME)
        print('Found role: ' + LAMBDA_EXECUTION_ROLE_NAME + '.')

        policy = result['get_role_response']['get_role_result']['role']['assume_role_policy_document']
        if (
            policy is not None and
            json.dumps(json.loads(unquote(policy)), sort_keys=True) == target_policy
        ):
            print('Assume role policy for: ' + LAMBDA_EXECUTION_ROLE_NAME + ' verified.')
        else:
            print('Updating assume role policy for: ' + LAMBDA_EXECUTION_ROLE_NAME + '.')
            iam.update_assume_role_policy(LAMBDA_EXECUTION_ROLE_NAME, target_policy)
            time.sleep(WAIT_TIME)
    except BotoServerError:
        print('Creating role: ' + LAMBDA_EXECUTION_ROLE_NAME + '...')
        iam.create_role(
            LAMBDA_EXECUTION_ROLE_NAME,
            assume_role_policy_document=target_policy
        )
        result = iam.get_role(LAMBDA_EXECUTION_ROLE_NAME)

    role_arn = result['get_role_response']['get_role_result']['role']['arn']

    return role_arn


def check_lambda_execution_role_policies():
    iam = get_iam_connection()

    response = iam.list_role_policies(LAMBDA_EXECUTION_ROLE_NAME)
    policy_names = response['list_role_policies_response']['list_role_policies_result']['policy_names']

    found = False
    for p in policy_names:
        found = (p == LAMBDA_EXECUTION_ROLE_POLICY_NAME)
        if found:
            print('Found policy: ' + LAMBDA_EXECUTION_ROLE_POLICY_NAME + '.')
            break

    if not found:
        print('Attaching policy: ' + LAMBDA_EXECUTION_ROLE_POLICY_NAME + '.')
        iam.put_role_policy(
            LAMBDA_EXECUTION_ROLE_NAME,
            'AWSLambdaExecute',
            json.dumps(LAMBDA_EXECUTION_ROLE_POLICY)
        )

    return


def get_lambda_function_arn():
    result = json.loads(
        local(
            'aws lambda list-functions' +
            AWS_CLI_STANDARD_OPTIONS,
            capture=True
        )
    )
    if result is not None and isinstance(result, dict):
        for f in result.get('Functions', []):
            if f['FunctionName'] == LAMBDA_FUNCTION_NAME:
                return f['FunctionArn']

    return None


def delete_lambda_function():
    local(
        'aws lambda delete-function' +
        '    --function-name ' + LAMBDA_FUNCTION_NAME +
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    )


def update_lambda_function():
    dump_lambda_function_configuration()
    create_lambda_deployment_package()
    role_arn = get_or_create_lambda_execution_role()
    check_lambda_execution_role_policies()

    if get_lambda_function_arn() is not None:
        print('Deleting existing Lambda function ' + LAMBDA_FUNCTION_NAME + '.')
        delete_lambda_function()

    local(
        'aws lambda create-function' +
        '    --function-name ' + LAMBDA_FUNCTION_NAME +
        '    --zip-file fileb://./' + ZIPFILE_NAME +
        '    --role ' + role_arn +
        '    --handler ' + LAMBDA_FUNCTION_NAME + '.handler' +
        '    --runtime nodejs' +
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    )


def show_lambda_execution_role_policy():
    print json.dumps(LAMBDA_EXECUTION_ROLE_POLICY, sort_keys=True)


# Amazon S3


def get_s3_connection():
    aws_access_key_id, aws_secret_access_key = get_aws_credentials()
    return boto.s3.connect_to_region(
        AWS_REGION,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key
    )


def get_or_create_bucket():
    s3 = get_s3_connection()
    b = s3.lookup(AWS_BUCKET)
    if b is None:
        print('Creating bucket: ' + AWS_BUCKET + ' in region: ' + AWS_REGION + '...')
        LOCATION = AWS_REGION if AWS_REGION != 'us-east-1' else ''
        b = s3.create_bucket(AWS_BUCKET, location=LOCATION, policy='public-read')
        b.set_acl('public-read')
        b.configure_website('index.html', 'error.html')
        set_bucket_policy(b)
        set_bucket_role_policy()
    else:
        print('Found bucket: ' + AWS_BUCKET + '.')

    return b


def set_bucket_policy(bucket):
    jsonstr = ANON_BUCKET_ACCESS_POLICY.copy()
    jsonstr["Statement"][0]["Resource"] = 'arn:aws:s3:::' + AWS_BUCKET + '/*'
    policy = json.dumps(jsonstr)
    while True:
        if (bucket.set_policy(policy)):
            break
        print("set policy retry...")
        time.sleep(WAIT_TIME)



def set_bucket_role_policy():
    role = get_instance_role()
    policy = json.dumps(generate_ecs_role_policy())
    iam = get_iam_connection()
    iam.create_role(role)
    print('Putting policy: ' + ECS_ROLE_BUCKET_ACCESS_POLICY_NAME + ' into role: ' + role)
    iam.put_role_policy(
        role,
        ECS_ROLE_BUCKET_ACCESS_POLICY_NAME,
        policy
    )


def generate_ecs_role_policy():
    result = ECS_ROLE_BUCKET_ACCESS_POLICY.copy()
    result['Statement'][1]['Resource'] = 'arn:aws:s3:::' + AWS_BUCKET
    result['Statement'][2]['Resource'] = 'arn:aws:s3:::' + AWS_BUCKET + '/*'
    return result


def check_bucket_permissions():
    with quiet():
        result = local(
            'aws lambda get-policy' +
            '    --function-name ' + LAMBDA_FUNCTION_NAME +
            AWS_CLI_STANDARD_OPTIONS,
            capture=True
        )

    if result.failed or result == '':
        return False

    result_decoded = json.loads(result)
    if not isinstance(result_decoded, dict):
        return False

    policy = json.loads(result_decoded.get('Policy', '{}'))
    if not isinstance(policy, dict):
        return False

    statements = policy.get('Statement', [])
    for s in statements:
        if s.get('Sid', '') == BUCKET_PERMISSION_SID:
            return True

    return False


def update_bucket_permissions():
    get_or_create_bucket()
    if check_bucket_permissions():
        print('Lambda invocation permission for bucket: ' + AWS_BUCKET + ' is set.')
    else:
        print('Setting Lambda invocation permission for bucket: ' + AWS_BUCKET + '.')
        local(
            'aws lambda add-permission' +
            '    --function-name ' + LAMBDA_FUNCTION_NAME +
            '    --region ' + AWS_REGION +
            '    --statement-id ' + BUCKET_PERMISSION_SID +
            '    --action "lambda:InvokeFunction"' +
            '    --principal s3.amazonaws.com' +
            '    --source-arn arn:aws:s3:::' + AWS_BUCKET +
            '    --profile ' + AWS_PROFILE,
            capture=True
        )


def check_bucket_notifications():
    result = local(
        'aws s3api get-bucket-notification-configuration' +
        '    --bucket ' + AWS_BUCKET +
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    )

    if result.failed or result == '':
        return False

    result_decoded = json.loads(result)
    if not isinstance(result_decoded, dict):
        return False


def setup_bucket_notifications():
    update_lambda_function()
    update_bucket_permissions()

    notification_configuration = BUCKET_NOTIFICATION_CONFIGURATION.copy()
    lambda_function_arn = get_lambda_function_arn()
    notification_configuration['LambdaFunctionConfigurations'][0]['LambdaFunctionArn'] = lambda_function_arn

    if check_bucket_notifications():
        print('Bucket notification configuration for bucket: ' + AWS_BUCKET + ' is set.')
    else:
        print('Setting bucket notification configuration for bucket: ' + AWS_BUCKET + '.')
        local(
            'aws s3api put-bucket-notification-configuration' +
            '    --bucket ' + AWS_BUCKET +
            '    --notification-configuration \'' + json.dumps(notification_configuration, sort_keys=True) + '\'' +
            AWS_CLI_STANDARD_OPTIONS,
            capture=True
        )


# Amazon EC2

def get_instance_ip_from_id(instance_id):
    result = json.loads(local(
        'aws ec2 describe-instances' +
        '    --instance ' + instance_id +
        '    --query Reservations[0].Instances[0].PublicIpAddress' +
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    ))
    print ('IP address for instance ' + instance_id + ' is: ' + result)
    return result


def get_instance_profile_name(instance_id):
    result = json.loads(local(
        'aws ec2 describe-instances' +
        '    --instance ' + instance_id +
        '    --query Reservations[0].Instances[0].IamInstanceProfile.Arn' +
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    )).split('/')[-1]
    print('IAM instance profile for instance ' + instance_id + ' is: ' + result)
    return result


def get_instance_role():
    return ECS_ROLE_BUCKET_ACCESS_ROLE_NAME


# Amazon ECS


def get_container_instances():
    result = json.loads(local(
        'aws ecs list-container-instances' +
        '    --query containerInstanceArns' +
        '    --cluster ' + ECS_CLUSTER + 
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    ))
    print('Container instances: ' + ','.join(result))
    return result


# Amazon SQS


def get_queue_url():
    result = local(
        'aws sqs list-queues' +
        AWS_CLI_STANDARD_OPTIONS,
        capture=True
    )

    if result is not None and result != '':
        result_struct = json.loads(result)
        if isinstance(result_struct, dict) and 'QueueUrls' in result_struct:
            for u in result_struct['QueueUrls']:
                if u.split('/')[-1] == SQS_QUEUE_NAME:
                    return u

    return None


def get_or_create_queue():
    u = get_queue_url()
    if u is None:
        local(
            'aws sqs create-queue' +
            '    --queue-name ' + SQS_QUEUE_NAME +
            AWS_CLI_STANDARD_OPTIONS,
            capture=True
        )

        tries = 0
        while True:
            time.sleep(WAIT_TIME)
            u = get_queue_url()

            if u is not None and tries < RETRIES:
                return u

            tries += 1

# High level functions. Call these as "fab <function>"


def update_bucket():
    get_or_create_bucket()


def update_lambda():
    update_lambda_function()
    update_bucket_permissions()
    setup_bucket_notifications()


def update_queue():
    get_or_create_queue()


def setup():
    update_dependencies()
    update_bucket()
    update_queue()
    update_lambda()


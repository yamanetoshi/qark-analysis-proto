#!/usr/bin/python
# coding: utf-8

# Imports

from fabric.api import env, lcd
from fabric.api import local as local_cmd
import boto
import boto.s3
import boto.sqs
from boto.exception import BotoServerError
import os
import json
import datetime

# Constants (User configurable), imported from config.py

from config import *

def local(command_string):
    local_cmd(command_string, shell="/bin/bash")

def setup():
    retry = 0
    region=AWS_REGION
    queue=SQS_QUEUE_URL

    conn = boto.sqs.connect_to_region(region)

    # Fetch messages and render them until the queue is drained.
    while True:
        # Fetch the next message and extract the S3 URL to fetch the POV-Ray source ZIP from.
        print "Fetching messages fom SQS queue: ${queue}..."
        q = boto.sqs.queue.Queue(conn, queue)

        rs = q.get_messages()
        if (len(rs) == 0):
            if (retry > 5):
                break
            else:
                retry += 1
                continue

        obj = json.loads(rs[0].get_body())

        print "Message: %s." % str(obj)

        receipt_handle = q
        print "Receipt handle: %s." % (str(receipt_handle))

        bucket = obj['Records'][0]['s3']['bucket']['name']
        print "Bucket: %s." % (bucket)

        key = obj['Records'][0]['s3']['object']['key']
        print "Key: %s." % (str(key))

        base, ext = os.path.splitext(key)
        ext = ext[1:]

        if (str(obj) and str(receipt_handle) and str(bucket) and str(key) and base and ext and ext == "apk"):

            local('mkdir -p work')
            lcd('./work')

            print "Copying +key+ from S3 bucket +bucket+..."
            s3str = "aws s3 cp s3://%s/%s . --region %s" % (bucket, key, region)
            local(s3str)

            analysisStr = "python /usr/local/qark/qark.py -s 1 --pathtoapk ./%s -e 0 -r /tmp -d 50" % (key)
            local(analysisStr)
            DATESTR = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
            URL="http://%s.s3-website-%s.amazonaws.com/%s/report.html" % (bucket, region, DATESTR)
            s3syncStr = "aws s3 sync ./report s3://%s/%s" % (bucket , DATESTR)
            local(s3syncStr)
            curlStr = "curl -XPOST -d \"token=%s\" -d \"channel=#%s\" -d \"text=%s\" -d \"username=bot\" \"https://slack.com/api/chat.postMessage\"" % (SLACK_ACCESS_TOKEN, SLACK_CHANNEL, URL)
            local(curlStr)

            print "Cleaning up..."
            lcd('..')
            local('/bin/rm -rf work')

            print "Deleting message..."

            q.delete_message(rs[0])
        else:
            print "message error..."
            break

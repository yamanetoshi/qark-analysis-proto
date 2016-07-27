#!/bin/bash

cd /usr/local/qark-analysis

. ./config.sh
if [ -z "$_START" ] ;
then
  logger "kickoff.sh cannot start"
  exit 1
fi

docker build --no-cache -t analysis .
docker run -t analysis


#!/bin/sh

echo "Creating buckets"

set -e
influx bucket create -n high_rate
influx bucket create -n low_rate

HIGH_RATE_ID=$(influx bucket ls | grep high_rate | awk '{print $1}')
LOW_RATE_ID=$(influx bucket ls | grep low_rate | awk '{print $1}')

influx auth create \
  --org ${ORG} \
  --read-bucket ${HIGH_RATE_ID} \
  --read-bucket ${LOW_RATE_ID} \
  --write-bucket ${HIGH_RATE_ID} \
  --write-bucket ${LOW_RATE_ID} \
  --description "Read/write token for high_rate and low_rate"

influx auth create \
  --org ${ORG} \
  --read-bucket ${HIGH_RATE_ID} \
  --read-bucket ${LOW_RATE_ID} \
  --description "Read-only token for high_rate and low_rate"

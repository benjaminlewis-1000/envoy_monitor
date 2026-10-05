#!/bin/sh

# Set environment variables
export MY_VARIABLE="my_value"
export ANOTHER_VARIABLE="another_value"

# Execute the main container command
# exec "$@"

influxd

#!/bin/bash

OUTPUT_FILE=$1
IPERF_SERVER=$2

# -b is bit rate (e.g. 1M is 1M bits)
# -t is number of seconds to run test for
# -u is udp
# -p is port
# -J is XML output
iperf3 -c $IPERF_SERVER -u -b1M -t 10 -p 6201 -J  >> $OUTPUT_FILE



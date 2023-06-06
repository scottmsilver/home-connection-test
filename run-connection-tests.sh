#!/bin/bash

# Directory of this script.
# From https://stackoverflow.com/questions/59895/how-to-get-the-source-directory-of-a-bash-script-from-within-the-script-itself
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PORT=${1:-6202}
IFACE_NAME=${2:-"unknown"}
IPERF_SEVER="foo"
IPERF_USERNAME="username"
IPERF_PASSWORD="password"
TIMEOUT=10

source $DIR/env/bin/activate
source $DIR/config.env
python3 $DIR/udptest-telegraf.py --iperf-server $IPERF_SERVER --iperf-server-port $PORT --iperf-public-key-file $DIR/public.pem --iperf-username=$IPERF_USERNAME --iperf-password=$IPERF_PASSWORD --iperf-upload-mbits=3 --timeout=$TIMEOUT --interface-name=$IFACE_NAME

#timeout 30 python3 $DIR/tcptest-telegraf.py --iperf-server $IPERF_SERVER --iperf-server-port $PORT --iperf-public-key-file $DIR/public.pem --iperf-username=$IPERF_USERNAME --iperf-password=$IPERF_PASSWORD --iperf-mode download

#timeout 30 python3 $DIR/tcptest-telegraf.py --iperf-server $IPERF_SERVER --iperf-server-port $PORT --iperf-public-key-file $DIR/public.pem --iperf-username=$IPERF_USERNAME --iperf-password=$IPERF_PASSWORD --iperf-mode upload

#!/bin/bash

# Directory of this script.
# From https://stackoverflow.com/questions/59895/how-to-get-the-source-directory-of-a-bash-script-from-within-the-script-itself
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PORT=${1:-6202}


source $DIR/environment/bin/activate
LD_LIBRARY_PATH=/usr/local/lib python $DIR/collect-data.py --iperf-server 35.224.53.38 --iperf-server-port $PORT --iperf-public-key-file $DIR/public.pem --iperf-username=ssilver --iperf-password=316costello --speedtest-csv-file=/var/log/speedtest.csv --iperf-csv-file=/var/log/iperf.csv

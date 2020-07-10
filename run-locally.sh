#!/bin/bash

# You may not need to change this unless you want to.
OUTPUT_DIRECTORY="/tmp"
IPERF_SERVER=35.224.53.38 

# You shouldn't need to change these.
SPEEDTEST_CSV=speedtest.csv
IPERF_XML=iperf.xml
SPEEDTEST_CSV_PATH="$OUTPUT_DIRECTORY/$SPEEDTEST_CSV"
IPERF_XML_PATH="$OUTPUT_DIRECTORY/$IPERF_XML"

while [ 1 ]
do
    echo "Collecting data"
    ./collect-data.sh $SPEEDTEST_CSV_PATH $IPERF_XML_PATH $IPERF_SERVER
    echo "Graphing data"
    ./graph-data.sh $OUTPUT_DIRECTORY $SPEEDTEST_CSV_PATH $IPERF_XML_PATH
    echo "Sleeping'
    sleep 300
done

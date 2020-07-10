#!/bin/bash
GRAPH_OUTPUT_DIRECTORY=$1
SPEEDTEST_CSV_FILE=$2
IPERF_XML_FILE=$3

python graph-speedtest.py  $SPEEDTEST_CSV_FILE Download $GRAPH_OUTPUT_DIRECTORY/download.png
python graph-speedtest.py  $SPEEDTEST_CSV_FILE Upload $GRAPH_OUTPUT_DIRECTORY/upload.png
python graph-packetloss.py $IPERF_XML_FILE $GRAPH_OUTPUT_DIRECTORY/packet-loss.png

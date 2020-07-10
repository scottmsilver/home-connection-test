#!/bin/bash

PROBE_DEST_DIRECTORY=/tmp
PROBE_SOURCE_DIRECTORY=/root
PROBE_HOST=192.168.1.1
PROBE_SPEEDTEST_OUTPUT_FILEPATH=/root/testresults
PROBE_IPERFTEST_OUTPUT_FILEPATH=/root/iperfresults

while [ 1 ]
do
  scp PROBE_USER@PROBE_HOST:"$PROBE_SPEEDTEST_OUTPUT_FILEPATH $PROBE_IPERFTEST_OUTPUT_FILEPATH $PROBE_DEST_DIRECTORY
    
    scp PROBE_USER@$PROBE_HOST:/root/testresults 
  scp speed.png root@192.168.1.1:/usr/local/www
  scp root@192.168.1.1:/root/iperfresults iperfresults
  python packetloss.py
  scp packets.png root@192.168.1.1:/usr/local/www
  sleep 300
done

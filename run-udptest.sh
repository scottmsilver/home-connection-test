#!/bin/bash
IPERF_SERVER="foo"
IPERF_USERNAME="username"
IPERF_PASSWORD="password"
TIMEOUT=10

while [[ $# -gt 0 ]]; do
  key="$1"
  shift

  case $key in
    -p|--port)
      PORT="$1"
      shift
      ;;
    -i|--interface)
      INTERFACE_NAME="$1"
      shift
      ;;
    *)
      echo "Unknown option: $key"
      exit 1
      ;;
  esac
done

# /config/config.env will override server, username and password 
# /config/public.pem is your public key
source /config/config.env
python3 udptest-telegraf.py --iperf-server $IPERF_SERVER --iperf-server-port $PORT \
  --iperf-public-key-file /config/public.pem --iperf-username=$IPERF_USERNAME \
  --iperf-password=$IPERF_PASSWORD --iperf-upload-mbits=3 --timeout=$TIMEOUT \
  --interface-name=$INTERFACE_NAME

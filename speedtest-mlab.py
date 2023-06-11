from influx_line_protocol import Metric
import sys
import subprocess
import json
import argparse

# Run mlab speedtest and output a telegraf line format result inside of
# a network namespace using the nets-exec tool, which is a wrapper around
# the ip netns exec command, but runs as non root user.
# docker run --cap-add=NET_ADMIN -it ssilver/alpine-smarter:1.0 bash 
# iptables -t mangle -A POSTROUTING -j DSCP --set-dscp-class AF12
# curl -s ipinfo.io/ip
#
# Takes two argumennts, the interface nickname and the network namespace to run the test in and
# the path to ndt7-client.
#
# In this case att is just a nickname of an interface, and att is a real network namespace.
# that you setup before this.
#
# You must install netns-esec and ndt7-client.
#
# To install ndt7-client (install go first if necessary)
#
# git clone https://github.com/m-lab/ndt7-client-go.git
# cd ndt7-client-go
# CGO_ENABLED=0 go build ./cmd/ndt7-client/
# ndt7-client will be in the root
#
# To install netns-exec:
# git clone https://github.com/pekman/netns-exec 
# cd netns-exec 
# git submodule update --init
# make
# sudo make install

# Runs a single speedtest and outputs the results in telegraf line format.
def runSpeedtestTest(desired_interface, ndt7_binary, dscp_class):
  metric = Metric("speedtest")
  # Presume we will fail.
  metric.add_value("result", "FAIL")
  metric.add_value("download_bps", 0.0)
  metric.add_value("upload_bps", 0.0)


  metric.add_tag("desired_interface", desired_interface)
  metric.add_tag("actual_interface", "unknown")

  try:
    # optionally set the dscp class of outbound traffic, this presumes we are running as root and or can use iptables.
    if dscp_class:
      process = subprocess.run([
        f"iptables",
        "-t", "mangle",
        "-A", "POSTROUTING",
        "-j", "DSCP",
        "--set-dscp-class", dscp_class], 
        capture_output = True, check = True)
      process.check_returncode()

    # call a a go program to measure the speed
    process = subprocess.run([
      f"{ndt7_binary}",
      "--format=json",
      "--quiet"], 
      capture_output = True, check = True)
    # Example response
    # {"ServerFQDN":"ndt-mlab1-nuq06.mlab-oti.measurement-lab.org","ServerIP":"128.177.33.22","ClientIP":"32.141.34.33","Download":{"UUID":"ndt-th5mz_1685206528_00000000000906C2","Throughput":{"Value":45.48010318961437,"Unit":"Mbit/s"},"Latency":{"Value":5.949,"Unit":"ms"},"Retransmission":{"Value":0,"Unit":"%"}},"Upload":{"UUID":"ndt-3","Throughput":{"Value":40.831127814180604,"Unit":"Mbit/s"},"Latency":{"Value":6.486,"Unit":"ms"},"Retransmission":{"Value":0,"Unit":""}}}
    process.check_returncode()
    results = json.loads(process.stdout)

    metric.add_value("result", "SUCCESS")
    metric.add_tag("actual_interface", results["ClientIP"])
    metric.add_value("download_bps", results["Download"]['Throughput']['Value'])
    metric.add_value("upload_bps", results["Upload"]['Throughput']['Value'])
  except Exception as e:
    metric.add_value("error_text", repr(e))
  finally:
    print(metric)

def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("-i", "--desired_interface", help="The desired interface")
  parser.add_argument("-b", "--ndt7_binary", help="The NDT7 binary")
  parser.add_argument("-d", "--dscp_class", help="The DSCP class")
  args = parser.parse_args()

  if not args.desired_interface:
    print("Please specify the desired interface")
    return

  if not args.ndt7_binary:
    print("Please specify the NDT7 binary")
    return
  
  runSpeedtestTest(args.desired_interface, args.ndt7_binary, args.dscp_class)

main()
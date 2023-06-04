from influx_line_protocol import Metric
import sys
import subprocess
import json

# Run mlab speedtest and output a telegraf line format result inside of
# a network namespace using the nets-exec tool, which is a wrapper around
# the ip netns exec command, but runs as non root user.
#
# Takes two argumennts, the interface nickname and the network namespace to run the test in.
# e.g.
#
# In this case att is just a nickname of an interface, and att is a real network namespace.
# that you setup before this.
#
# You must install https://github.com/pekman/netns-exec first.
#
# git clone https://github.com/pekman/netns-exec 
# git submodule update --init
# make
# sudo make install
def runSpeedtestTest():
  metric = Metric("speedtest")
  # Presume we will fail.
  metric.add_value("result", "FAIL")
  metric.add_value("download_bps", 0.0)
  metric.add_value("upload_bps", 0.0)

  ifname = sys.argv[1] 
  network_namespace = sys.argv[2]
  ndt7_client_root = sys.argv[3] # path for ndt7-client-go

  metric.add_tag("desired-interface", ifname)
  metric.add_tag("actual-interface", "unknown")

  try:
    # call a a go program to measure the speed
    process = subprocess.run([
      "netns-exec", network_namespace,
      "/usr/bin/go",
      "run",
      f"{ndt7_client_root}/cmd/ndt7-client/main.go",
      "--format=json",
      "--quiet"], 
      cwd=ndt7_client_root, capture_output = True, check = True)
    # Example response
    # {"ServerFQDN":"ndt-mlab1-nuq06.mlab-oti.measurement-lab.org","ServerIP":"128.177.33.22","ClientIP":"32.141.34.33","Download":{"UUID":"ndt-th5mz_1685206528_00000000000906C2","Throughput":{"Value":45.48010318961437,"Unit":"Mbit/s"},"Latency":{"Value":5.949,"Unit":"ms"},"Retransmission":{"Value":0,"Unit":"%"}},"Upload":{"UUID":"ndt-3","Throughput":{"Value":40.831127814180604,"Unit":"Mbit/s"},"Latency":{"Value":6.486,"Unit":"ms"},"Retransmission":{"Value":0,"Unit":""}}}
    process.check_returncode()
    results = json.loads(process.stdout)

    metric.add_value("result", "SUCCESS")
    metric.add_tag("actual-interface", results["ClientIP"])
    metric.add_value("download_bps", results["Download"]['Throughput']['Value'])
    metric.add_value("upload_bps", results["Upload"]['Throughput']['Value'])
  except Exception as e:
    print(e)
    metric.add_value("error_text", repr(e))
  finally:
    print(metric)
  
runSpeedtestTest()

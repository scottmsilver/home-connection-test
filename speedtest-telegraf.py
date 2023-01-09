#!/usr/bin/python3
from influx_line_protocol import Metric
import speedtest
import sys
import re
import subprocess

# return ip address associated with ifname
def get_ip_address(ifname):
  ifconfig_output = subprocess.run(['ifconfig', ifname], stdout=subprocess.PIPE).stdout.decode('utf-8')
  m = re.search('\w*inet ([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)', ifconfig_output)
  return m.group(1)

# Run speedtest and output a telegraf line format result
def runSpeedtestTest():
  metric = Metric("speedtest")
  # Presume we will fail.
  metric.add_tag("result", "FAIL")
  metric.add_value("download_bps", 0.0)
  metric.add_value("upload_bps", 0.0)

  ifname = sys.argv[1] 
  metric.add_tag("interface", ifname)

  # find ip address associated with this interface 
  ip_address = get_ip_address(ifname)
 
  try:
    s = speedtest.Speedtest(secure = True, source_address = ip_address)
    s.get_servers()
    s.get_best_server()
    s.download()
    s.upload()
    results = s.results.dict()
    metric.add_tag("result", "SUCCESS")
    metric.add_tag("client", results["client"]['ip'])
    metric.add_value("download_bps", results['download'])
    metric.add_value("upload_bps", results['upload'])
  except Exception as e:
    metric.add_value("error_text", repr(e))
  finally:
    print(metric)
  

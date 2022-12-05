#!/usr/bin/python3
from influx_line_protocol import Metric
from random import randint
import speedtest
import sys

# Run speedtest and output a telegraf line format result
def runSpeedtestTest():
  metric = Metric("speedtest")
  # Presume we will fail.
  metric.add_tag("result", "FAIL")
  metric.add_value("download_bps", 0.0)
  metric.add_value("upload_bps", 0.0)

  # Select a source port int he range sourcePortStart to sourcePortStart + 999
  # This avoids the case waiting for the socked to close.
  sourcePortStart = 0 if len(sys.argv) == 1 else int(sys.argv[1])
  sourcePort = 0 if sourcePortStart == 0 else sourcePortStart + randint(0, 999)
  sourceAddress = '' if len(sys.argv) == 0 else ':{}'.format(sourcePort)
  
  try:
    s = speedtest.Speedtest(secure = True, source_address = sourceAddress)
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
  
runSpeedtestTest()

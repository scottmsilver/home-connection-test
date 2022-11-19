#!/usr/bin/python3

import speedtest
from metric import Metric
import sys

# Run speedtest and output a telegraf line format result
def runSpeedtestTest():
  metric = Metric("speedtest")
  # Presume we will fail.
  metric.add_tag("result", "FAIL")
  metric.add_value("download_bps", 0.0)
  metric.add_value("upload_bps", 0.0)
  
  try:
    s = speedtest.Speedtest(secure = True)
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
    print(e, file=sys.stderr)
  finally:
    print(metric)
  
runSpeedtestTest()

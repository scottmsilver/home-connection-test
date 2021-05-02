import speedtest
import metric

# Run speedtest and output a telegraf line format result
def runSpeedtestTest():
  metric = Metric("speedtest")
  # Presume we will fail.
  metric.add_tag("result", "FAIL")

  try:
    s = speedtest.Speedtest()
    s.get_servers()
    s.get_best_server()
    s.download()
    s.upload()
    results = s.results.dict()
    metric.add_tag("result", "SUCCESS")
    metric.add_tag("client", results["client"])
    metric.add_tag("download_bps", results['download'])
    metric.add_tag("upload_bps", results['upload'])
  finally:
    print(metric)
  
runSpeedtestTest()

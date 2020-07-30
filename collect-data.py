#!/usr/bin/env python3
import iperf3
import csv
import datetime
import speedtest
from influxdb import InfluxDBClient
import time

# Call iPerf to do a 1Mbit UDP stream for 5 seconds or 625K
# Return the iPerf result object.
def calliPerf(host, port):
  client = iperf3.Client(verbose=False)
  client.server_hostname = host
  client.port = port
  client.protocol = 'udp'
  client.bandwidth = 1 * 1000 * 1000
  client.duration = 5
  # iPerf tends to freak out it above this.
  client.blksize = 1408

  return client.run()

# Add row, a list of column values, to end of csvFile
def appendToCsv(csvFile, row):
  with open(csvFile, 'a') as f:
     writer = csv.writer(f)
     writer.writerow(row)

# Return parsed python date from iPerf date format
# iPerf date looks like: Wed, 29 Jul 2020 03:15:51 UTC
def parseiPerfDate(date):
 return datetime.datetime.strptime(date, '%a, %d %b %Y %H:%M:%S %Z')

# Write row to csvFile as result of calling iPerf server
# Format:
#   Time, OK|FAIL, Average_Megabits, PacketLossPercent
def runiPerfTest(csvFile, host, port):
  row = [datetime.datetime.now(datetime.timezone.utc), "FAIL", 0.0, 0.0]
  try:
    result = calliPerf(host, port)
    if not result.error:
      row = [parseiPerfDate(result.time), "OK", result.Mbps, result.lost_percent]
  finally:
    try:
      simpleWriteToInflux(row[0], "upload_packet_loss", { "STATUS": row[1], "UPLOAD_MBPS": row[2], "PACKET_LOST_PERCENT": float(row[3])})
    finally:
      appendToCsv(csvFile, row)

# Time contains trailing Z (for Zulu time that fromiso doesn't parse)
# Return a proper datetime from the format used by speedtest.
def parseSpeedtestDate(dateText):
 return datetime.datetime.fromisoformat(dateText[:-1])

# Run a speedtest and output the data to csvFile
# Time, OK|FAIL, Download Speed Bit/s, Upload Speed Bit/S
def runSpeedtestTest(csvFile):
  # Presume we will fail.
  row = [datetime.datetime.now(datetime.timezone.utc), "FAIL", 0.0, 0.0]

  try:
    s = speedtest.Speedtest()
    s.get_servers()
    s.get_best_server()
    s.download()
    s.upload()
    results = s.results.dict()

    row = [parseSpeedtestDate(results['timestamp']), "OK", results['download'], results['upload']]
  finally:
    try:
      simpleWriteToInflux(row[0], "speedtest", { "STATUS": row[1], "DOWNLOAD_BPS": row[2], "UPLOAD_BPS": row[3]})
    finally:
      appendToCsv(csvFile, row)

def simpleWriteToInflux(timestamp, measurementName, values):
  json_body = [ {
    "measurement": measurementName,
    "TIMESTAMP": timestamp,
    "fields": values
  } ]

  client = InfluxDBClient('localhost', 8086, 'root', 'root', 'example')
  client.write_points(json_body)

while True:
  print("running speetest");
  runSpeedtestTest('speedtest.csv')
  print("running iperftest");
  runiPerfTest('iperf.csv', '35.224.53.38', 6201)
  print("sleeping for 5 minutes")
  time.sleep(300)
  


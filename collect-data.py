#!/usr/bin/env python3
import iperf3
import csv
import datetime
import speedtest
from influxdb import InfluxDBClient
import time
import argparse
import socket
import re
import base64

# Add row, a list of column values, to end of csvFile
def appendToCsv(csvFile, row):
  with open(csvFile, 'a') as f:
     writer = csv.writer(f)
     writer.writerow(row)

# Return the contents of a PEM file B64 encoded
def getKeyAsBase64(keyFile):
    with open(keyFile, 'rb') as file:
        return base64.b64encode(file.read()).decode("ascii")

# Class used to test the network and write the results to a Csv file and InfluxDb
class NetworkTester:
    def __init__(self, influxDbClient, iperfCsvFile, iperfServer, iperfServerPort, iperfServerUploadMbits, iperfServerUploadDuration, speedtestCsvFile, server, iperfUsername, iperfPassword, iperfPublicKeyFile):
        self.influxDbClient = influxDbClient
        self.iperfCsvFile = iperfCsvFile
        self.iperfServer = iperfServer
        self.iperfServerPort = iperfServerPort
        self.iperfServerUploadMbits = iperfServerUploadMbits
        self.iperfServerUploadDuration = iperfServerUploadDuration
        self.speedtestCsvFile = speedtestCsvFile
        self.server = server
        self.iperfPassword = iperfPassword
        self.iperfUsername = iperfUsername
        self.iperfPublicKeyFile = iperfPublicKeyFile

    # Write a measurement to InfluxDB
    def simpleWriteToInflux(self, timestamp, measurementName, localHost, values):
      jsonBody = [ {
        "measurement": measurementName,
        "TIMESTAMP": timestamp,
        "tags": {
            "SERVER" : self.server,
            "LOCAL_HOST" : localHost
        },
        "fields": values
      } ]

      self.influxDbClient.write_points(jsonBody)

    # Make an iPerf UDP connection of the desired size and duration.
    # Return the iPerf result object.
    def calliPerf(self):
      client = iperf3.Client(verbose=False)
      client.server_hostname = self.iperfServer
      client.port = self.iperfServerPort
      client.protocol = 'udp'
      client.bandwidth = self.iperfServerUploadMbits * 1000 * 1000
      client.duration = self.iperfServerUploadDuration

      if self.iperfUsername and self.iperfPassword and self.iperfPublicKeyFile:
          client.username = self.iperfUsername
          client.password = self.iperfPassword
          client.rsa_pubkey = getKeyAsBase64(self.iperfPublicKeyFile)

      # iPerf tends to freak out it above this.
      client.blksize = 1408

      # We'll use this to figure out what IP we actually connected from.
      client.server_output = True

      return client.run()

    # Return parsed python date from iPerf date format
    # iPerf date looks like: Wed, 29 Jul 2020 03:15:51 UTC
    def parseiPerfDate(self, date):
     return datetime.datetime.strptime(date, '%a, %d %b %Y %H:%M:%S %Z')

    # Write row to csvFile as result of calling iPerf server
    # Format:
    #   Time, OK|FAIL, Average_Megabits, PacketLossPercent
    def runiPerfTest(self):
      # row[4] is the host that the server things is requesting things
      row = [datetime.datetime.now(datetime.timezone.utc), "FAIL", 0.0, 100.0, "0.0.0.0"]
      try:
        result = self.calliPerf()
        if not result.error:
          # The server output contains where the server accepted a connection from.
          # In some setups the client may exit through multiple IP addresses so we want to record
          # the actual public egress / ingress point from the client.
          p = re.compile('Accepted connection from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})')
          m = p.search(result.server_output_text)
          connection_ip_address = m.group(1)
          row = [self.parseiPerfDate(result.time), "OK", result.Mbps, result.lost_percent, connection_ip_address]
          print("success to %s" % connection_ip_address)
        else:
          print("iperf failed: " + result.error)
      finally:
        try:
          self.simpleWriteToInflux(row[0], "upload_packet_loss", row[4], { "STATUS": row[1], "UPLOAD_MBPS": row[2], "PACKET_LOST_PERCENT": float(row[3]) })
        finally:
          appendToCsv(self.iperfCsvFile, row)

    # Time contains trailing Z (for Zulu time that fromiso doesn't parse)
    # Return a proper datetime from the format used by speedtest.
    def parseSpeedtestDate(self, dateText):
     return datetime.datetime.fromisoformat(dateText[:-1])

    # Run a speedtest and output the data to csvFile
    # Time, OK|FAIL, Download Speed Bit/s, Upload Speed Bit/S
    def runSpeedtestTest(self):
      # Presume we will fail.
      row = [datetime.datetime.now(datetime.timezone.utc), "FAIL", 0.0, 0.0, "0.0.0.0"]

      try:
        s = speedtest.Speedtest()
        s.get_servers()
        s.get_best_server()
        s.download()
        s.upload()
        results = s.results.dict()

        row = [self.parseSpeedtestDate(results['timestamp']), "OK", results['download'], results['upload'], results['client']['ip']]
      finally:
        try:
          self.simpleWriteToInflux(row[0], "speedtest", row[4], { "STATUS": row[1], "DOWNLOAD_BPS": row[2], "UPLOAD_BPS": row[3] })
        finally:
          appendToCsv(self.speedtestCsvFile, row)


parser = argparse.ArgumentParser(description='Collect performance data.')
parser.add_argument('--iperf-csv-file', default='iperf.csv', help = 'Name of CSV file for iperf test')
parser.add_argument('--iperf-server', default='localhost', help = 'iperf3 server to use')
parser.add_argument('--iperf-username', default=None, help = 'iperf3 user name for authentication')
parser.add_argument('--iperf-password', default=None, help = 'iperf3 password for authentiation')
parser.add_argument('--iperf-public-key-file', default=None, help = 'iperf3 public key pem used to encrypt credentials to server')
parser.add_argument('--speedtest-csv-file', default='speedtest.csv', help = 'Name of CSV file for speedtest test')
parser.add_argument('--iperf-upload-mbits', default=1, type=int, help = 'Megabits to upload in the UDP test')
parser.add_argument('--iperf-server-port', default=5201, type=int, help = 'port of iperf3 server to which we should connect.')
parser.add_argument('--iperf-upload-duration', default=5, type=int, help = 'Seconds to run test for')
parser.add_argument('--pause-between-tests', default=300, type=int, help = 'Number of seconds to wait between tests')
parser.add_argument('--influx-db-hostname', default='localhost', help = 'InfluxDB hostname')
parser.add_argument('--influx-db-port', default=8086, help = 'InfluxDB port')
parser.add_argument('--influx-db-username', default='root', help = 'InfluxDB username')
parser.add_argument('--influx-db-password', default='root', help = 'InfluxDB password')
parser.add_argument('--influx-db-database-name', default='example', help = 'InfluxDB database')
parser.add_argument('--speedtest', dest = 'speedtestFeature', action='store_true')
parser.add_argument('--no-speedtest', dest = 'speedtestFeature', action='store_false')
parser.set_defaults(speedtestFeature = True)
parser.add_argument('--iperf', dest = 'iperfFeature', action='store_true')
parser.add_argument('--no-iperf', dest = 'iperfFeature', action='store_false')
parser.add_argument('--no-run-continuously', dest = 'runContinuously', action='store_false')
parser.add_argument('--run-continuously', dest = 'runContinuously', action='store_true')
parser.set_defaults(iperfFeature = True)
parser.add_argument('--server', default = socket.gethostname(), help='Name of server doing measurement')

args = parser.parse_args()
client = InfluxDBClient(args.influx_db_hostname, args.influx_db_port, args.influx_db_username, args.influx_db_password, args.influx_db_database_name)
tester = NetworkTester(client, args.iperf_csv_file, args.iperf_server, args.iperf_server_port, args.iperf_upload_mbits, args.iperf_upload_duration, args.speedtest_csv_file, args.server, args.iperf_username, args.iperf_password, args.iperf_public_key_file)
continueRunning = True

from threading import Timer
import time


class ResettableTimer(object):
    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.timer = Timer(self.interval, self.function)

    def run(self):
        self.timer.start()

    def reset(self):
        self.timer.cancel()
        self.timer = Timer(self.interval, self.function)
        self.timer.start()


while continueRunning:
   if args.speedtestFeature:
     try:
       print("running speedtest");
       tester.runSpeedtestTest()
     except Exception as e:
       print(e)
   if args.iperfFeature:
       try:
         print("running iperftest");
         tester.runiPerfTest()
       except Exception as e:
         print(e)
   if args.runContinuously:
     print("sleeping for %d seconds" % args.pause_between_tests)
     time.sleep(args.pause_between_tests)
   else:
     continueRunning = False

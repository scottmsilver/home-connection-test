import argparse
import socket
import re
from metric import Metric
import subprocess
import os
import json

class NetworkTester:
    def __init__(self, iperfServer, iperfServerPort, iperfServerUploadMbits, iperfServerUploadDuration, server, iperfUsername, iperfPassword, iperfPublicKeyFile):
        self.iperfServer = iperfServer
        self.iperfServerPort = iperfServerPort
        self.iperfServerUploadMbits = iperfServerUploadMbits
        self.iperfServerUploadDuration = iperfServerUploadDuration
        self.server = server
        self.iperfPassword = iperfPassword
        self.iperfUsername = iperfUsername
        self.iperfPublicKeyFile = iperfPublicKeyFile

    # Make an iPerf UDP connection of the desired size and duration.
    # Return the iPerf result object.
    # IPERF3_PASSWORD=PASSWORD iperf3 -c 35.224.173.82 -p 6202 --rsa-public-key-path public.pem --username "PASSWORD" -u -t 3 -J --get-server-output
    def calliPerf(self):
      import subprocess, os
      my_env = os.environ.copy()
      my_env["IPERF3_PASSWORD"] = self.iperfPassword
      return subprocess.run([
        'iperf3',
        '-c', self.iperfServer,
        '-p', "%d" % self.iperfServerPort,
        '--rsa-public-key-path', self.iperfPublicKeyFile,
        '--username', self.iperfUsername,
        '-u',
        '-t', "%d" % self.iperfServerUploadDuration,
        '-b', "%dM" % self.iperfServerUploadMbits,
        '-J',
        '--get-server-output'],
        env = my_env, capture_output = True, check = True)

    # Run and output telegraf line format for a single test.
    def runiPerfTest(self):
      metric = Metric("udptest")
      # Presume we will fail.
      metric.add_tag("result", "FAIL")
      metric.add_value("packet_lost_percent", 100.0)
  
      try:
        completedProcess = self.calliPerf()
        completedProcess.check_returncode()
        result = json.loads(completedProcess.stdout)
        # The server output contains where the server accepted a connection from.
        # In some setups the client may exit through multiple IP addresses so we want to record
        # the actual public egress / ingress point from the client.
        p = re.compile('Accepted connection from (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})')
        m = p.search(result['server_output_text'])
        connection_ip_address = m.group(1)
        summary = result['end']['sum']
        metric.add_tag("result", "SUCCESS")
        metric.add_tag("local_host", connection_ip_address)
        metric.add_value("packet_lost_percent", summary['lost_percent'])
        metric.add_value("jitter_ms", summary['jitter_ms'])
        metric.add_value("upload_mbps", summary['bits_per_second'])
      finally:
        print(metric)
  

parser = argparse.ArgumentParser(description='Collect performance data.')
parser.add_argument('--iperf-server', default='localhost', help = 'iperf3 server to use')
parser.add_argument('--iperf-username', default=None, help = 'iperf3 user name for authentication')
parser.add_argument('--iperf-password', default=None, help = 'iperf3 password for authentiation')
parser.add_argument('--iperf-public-key-file', default=None, help = 'iperf3 public key pem used to encrypt credentials to server')
parser.add_argument('--iperf-upload-mbits', default=1, type=int, help = 'Megabits to upload in the UDP test')
parser.add_argument('--iperf-server-port', default=5201, type=int, help = 'port of iperf3 server to which we should connect.')
parser.add_argument('--iperf-upload-duration', default=5, type=int, help = 'Seconds to run test for')
parser.add_argument('--server', default = socket.gethostname(), help='Name of server doing measurement')

args = parser.parse_args()
tester = NetworkTester(args.iperf_server, args.iperf_server_port, args.iperf_upload_mbits, args.iperf_upload_duration, args.server, args.iperf_username, args.iperf_password, args.iperf_public_key_file)

tester.runiPerfTest()

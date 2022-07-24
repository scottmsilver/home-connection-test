![Image of dashboard](https://github.com/scottmsilver/home-connection-test/blob/master/dashboard_example.png)

# About

home-connection-test tests your connection so you can understand how your home internet connection performs over time. It's especially focused on upstream bandwidth that is critical for video conferencing. Its goal is to simulate how video conferencing works and also raw "speedtests" that people use to talk about their internet connection. In particular, video conferencing typically requires high quality UDP streams. When zoom or meet see (upstream or downstream) latency or packet loss via UDP to their servers their clients will start degrading what they receive or send. 

# Overview

This package provides two scripts which output telegraf line format for monitoring connectivity. The basic idea is that run-udpscript.sh will output the "line format" that telegraf understands and hten you will be able to incorporate it into whatever kind of telegraf monitoring you have. 

# Installation

Ensure you have python3 installed

```
pi@raspberrypi:~ $ python3 -V
Python 3.7.3
```

NB: it is ok to have python2 installed (typically it's the default python). I recommend using virtualenv, but don't assume you do.

## Install pip3 (or make sure it's installed)

```
pi@raspberrypi:~ $ pip3 -V
pip 18.1 from /usr/lib/python3/dist-packages/pip (python 3.7)
```

or

debian: 

```
sudo apt-get install python3-pip
```

others go here https://pip.pypa.io/en/stable/installing/

## Install iperf

debian: 

```
sudo apt-get install iperf3
```

MacOS: 

```
brew install iperf
```

```
iperf3 --version
```
```
iperf 3.9+ (cJSON 1.7.13)
Linux measure-slc 5.4.0-59-generic #65-Ubuntu SMP Thu Dec 10 12:01:51 UTC 2020 x86_64
Optional features available: CPU affinity setting, IPv6 flow label, TCP congestion algorithm setting, sendfile / zerocopy, socket pacing, authentication, bind to device
```


## Install speedtest-cli

MacOS: 

```
brew install speedtest-cli
```

Debian: 

```
sudo apt-get install speedtest-cli
```

## Install speedtest python

```
pip3 install git+https://github.com/sivel/speedtest-cli.git
```

## Set-up iperf3 for authentication

Left as exercise to reader. This involves generating a public.pem and private.pem and assinging usernames and passwords.

## Create a config file and copy over your public key file.

Create a file called "config.env" in the same top-level directory (here's what it looks like)
```
$ cat config.env
IPERF_SERVER="SERVER"
IPERF_USERNAME="USERNAME"
IPERF_PASSWORD="PASSWORD"
```

Make sure your public key file called "public.pem" is in the same directory also.

## Test your udp test installation

```
$ ./run-udptest.sh 6201 # Assume your iperf3 server is running on 6201
udptest,result=SUCCESS,local_host=32.141.91.18 packet_lost_percent=0i,jitter_ms=0.0741303,upload_mbps=1.0002e+06
```

## Test your speedtest test installation

```
$ python3 speedtest-telegraf.py
speedtest,result=SUCCESS,client=76.103.100.176 download_bps=2.01315e+08,upload_bps=1.8573e+07
```

## Configure telegraf for influx (or whatever else you want to use for storage)

## Modify telegraf 

```
[[inputs.exec]]
   ## Commands array
   commands = [
     "/home/ssilver/home-connection-test/run-udptest.sh 6201",
     "/home/ssilver/home-connection-test/run-udptest.sh 6202",
     "/home/ssilver/home-connection-test/speedtest-telegraf.py"
   ]

   ## Timeout for each command to complete.
   timeout = "30s"
   interval = "1m"


   data_format = "influx"
```

## Test telegraf

Now verify that telegraf works. 

```
$ telegraf --test
> udptest,host=measure2,local_host=22.103.100.176,result=SUCCESS jitter_ms=2.55702,packet_lost_percent=0i,upload_mbps=1000210 1620012530000000000
> speedtest,client=22.103.100.176,host=measure2,result=SUCCESS download_bps=182191000,upload_bps=22561600 1620012547000000000
```

## Restart telegraf

Restart telegraf and then data should start flowing to however you configured your output (I use influx)

```
$ systemctl restart telegraf
```

## See your data in influx (if that's what you're using)

```
root@measure2:/home/ssilver/home-connection-test# influx
Connected to http://localhost:8086 version 1.8.5
InfluxDB shell version: 1.8.5
> use telegraf
Using database telegraf
> show measurements
name: measurements
name
----
cpu
disk
diskio
energy
gateways
hosts
kernel
mem
net
pf
ping
processes
procstat
purpleair
speedtest
swap
system
udptest
> select * from udptest limit 10
name: udptest
time                host     jitter_ms local_host     packet_lost_percent result  upload_mbps
----                ----     --------- ----------     ------------------- ------  -----------
1619994666000000000 measure2 0.0538207 32.141.91.18   0                   SUCCESS 1000220
1619994666000000000 measure2 2.64691   76.103.100.176 0                   SUCCESS 1000220
1619994726000000000 measure2 0.0400759 32.141.91.18   0                   SUCCESS 1000220
1619994726000000000 measure2 2.58087   76.103.100.176 0                   SUCCESS 1000220
1619994786000000000 measure2 0.0473156 32.141.91.18   0                   SUCCESS 1000220
1619994786000000000 measure2 2.63582   76.103.100.176 0                   SUCCESS 1000220
1619994846000000000 measure2 0.100663  32.141.91.18   0                   SUCCESS 1000220
1619994846000000000 measure2 2.07652   76.103.100.176 0                   SUCCESS 1000220
1619994906000000000 measure2 0.0533751 32.141.91.18   0                   SUCCESS 1000220
1619994906000000000 measure2 2.66971   76.103.100.176 0                   SUCCESS 1000200
```

## Now lets show some pretty graphs.

Install grafana the *OSS* (open source software version)

Debian: Debian release https://grafana.com/docs/grafana/latest/installation/debian/
Mac: https://grafana.com/docs/grafana/latest/installation/mac/
Use brew instructions here

```
brew install grafana
brew services start grafana
```

## Configure Grafana

Add Data Source (http://localhost:3000/datasources)

1. Type is InfluxDB
2. Server is http://localhost:8086
3. Database is "telegraf"
4. Click Test at bottom and it should work.

Example query

![Image of query](https://github.com/scottmsilver/home-connection-test/blob/master/query.png)

Example dashboards

![Image of dashboard](https://github.com/scottmsilver/home-connection-test/blob/master/dashboard_example.png)





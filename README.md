![Image of dashboard](https://github.com/scottmsilver/home-connection-test/blob/master/dashboard_example.png)

# About

home-connection-test tests your connection so you can understand how your home internet connection performs over time. It's especially focused on upstream bandwidth that is critical for video conferencing. Its goal is to simulate how video conferencing works and also raw "speedtests" that people use to talk about their internet connection. In particular, video conferencing typically requires high quality UDP streams. When zoom or meet see (upstream or downstream) latency or packet loss via UDP to their servers their clients will start degrading what they receive or send. I spent a fair time fighting with different (tcp) speed test type things too, and eventually settled on mlab.

# Overview

This package provides two scripts that I'd recommend you use from docker.

# Usage

## Docker speedtest

```
docker run --cap-add=NET_ADMIN scottmsilver/connection-test:1.0 python3 speedtest-mlab.py --desired_interface colony --ndt7_binary=ndt7-client --dscp_class=AF12
```

## Docker udptest

# Create config files

config.env is a text file

```
IPERF_SERVER=1.2.3.4
IPERF_USERNAME=X
IPERF_PASSWORD=Y
```

Make a public.pem (if you want it) is a public key file used by iperf3

Now put config.env and public.pem in the same directory and pass it on the command line as below.
Configure your port which is the port that iperf3 is listening on.

```
docker run -v /PATH/TO/CONFIG-FILES:/config scottmsilver/connection-test:1.0  bash run-udptest.sh --interface colony --port 6203
```


## Configure telegraf for influx (or whatever else you want to use for storage)

## Modify telegraf 

```
[[inputs.exec]]
   ## Commands array
   commands = [
     "docker run -v /PATH/TO/CONFIG-FILES:/config scottmsilver/connection-test:1.0  bash run-udptest.sh --interface colony --port 6203
",
     "docker run --cap-add=NET_ADMIN scottmsilver/connection-test:1.0 python3 speedtest-mlab.py --desired_interface colony --ndt7_binary=ndt7-client --dscp_class=AF12"
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

## See your data in influx (if that's what you're using) vs 1.8

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

## See your data in influx (if that's what you're using) vs 2.0+ InfluxQL

```
from(bucket: "telegraf")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r["_measurement"] == "udptest")
  |> filter(fn: (r) => r["_field"] == "packet_lost_percent")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
  
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





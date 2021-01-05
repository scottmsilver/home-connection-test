home connection test runs two kinds of connection tests and places the results in a csv and influx.

The first test runs a typical tcp speedtest. 
The second test uses iperf to run a udp test and record packet loss.
The general idea of the second is to simulate what it takes to use google meet or zoom.

For debian

```
sudo apt-get update
```

Ensure you have python3 installed

```
pi@raspberrypi:~ $ python3 -V
Python 3.7.3
```

It is ok to have python2 installed (typically it's the default python)

Ensure / Install pip3

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

Install iperf

debian: 

sudo apt-get install iperf3

MacOS: 

brew install iperf

Install iperf for python that I modified to support authentication (alternatively you can just install the regular version:

Download https://github.com/scottmsilver/home-connection-test/raw/master/iperf3-0.1.11.tar.gz

Check if iPerf installed supports authentication if you plan to use it and is version 3.8 or higher (notice "authentication" in the list below)

```
iperf3 --version
```
```
iperf 3.9+ (cJSON 1.7.13)
Linux measure-slc 5.4.0-59-generic #65-Ubuntu SMP Thu Dec 10 12:01:51 UTC 2020 x86_64
Optional features available: CPU affinity setting, IPv6 flow label, TCP congestion algorithm setting, sendfile / zerocopy, socket pacing, authentication, bind to device
```

```
pip3 install iperf3-0.1.11.tar.gz

```

Or use the regular version if you don't need authentication.

```
pip3 install iperf3
```


Install speedtest

MacOS: 

```
brew install speedtest-cli
```

Debian: 

```
sudo apt-get install speedtest-cli
```

Install speedtest python

```
pip3 install speedtest-cli
```

Install influxdb 

https://docs.influxdata.com/influxdb/v1.8/introduction/install/
Be sure to start the daemon running it running.

Test influx

```
influx
> show databases
name
----
_internal

> create database example
> show databases
show databases
name: databases
name
----
_internal
example
y
```

Install influx python adaptor

```
pip3 install influxdb
```

Now start collecting some data without authentication - send data by default to influxd running on localhost at 8086.
Be sure to change server and server port since you can't use mine :-)

```
python3 collect-data.py --iperf-server 35.224.53.38 --iperf-server-port 6201
```

Now start collecting data with authentication - be sure to change MYUSER and MYPASSWORD and the server certificate.

```
python3 collect-data.py --iperf-server 35.224.53.38 --iperf-server-port 6202 --iperf-public-key-file public.pem --iperf-username=MYUSER --iperf-password=MYPASSWORD --speedtest-csv-file=speedtest.csv --iperf-csv-file=iperf.csv
```

```
running speedtest
running iperftest
sleeping for 300 seconds
```

Now verify there is data 

```
$ influx

> use example
Using database example

> select * from speedtest limit 10
select * from speedtest limit 10

> show measurements

show measurements
name: measurements

name
----
speedtest
upload_packet_loss

> select * from speedtest
name: speedtest
time                DOWNLOAD_BPS       SERVER                STATUS UPLOAD_BPS
----                ------------       ------                ------ ----------
1596316554000083000 150367910.1738828  Scotts-Mac-mini.local OK     16206322.815829203
1596316884160920000 56917306.34614502  Scotts-Mac-mini.local OK     11493037.363126963
1596317211680887000 76242366.84952107  Scotts-Mac-mini.local OK     25434847.212901887
1596317538567693000 132096460.92245014 Scotts-Mac-mini.local OK     17117134.734898627
1596317865717493000 108213226.30501562 Scotts-Mac-mini.local OK     12293346.949994646
1596318192804164000 123236441.30121537 Scotts-Mac-mini.local OK     12232978.96974388
1596318520005486000 116783529.13795431 Scotts-Mac-mini.local OK     29744266.07239603
```

Now lets show some pretty graphs.

Install grafana the *OSS* (open source software version)

Debian: Debian release https://grafana.com/docs/grafana/latest/installation/debian/
Mac: https://grafana.com/docs/grafana/latest/installation/mac/
Use brew instructions here

```
brew install grafana
brew services start grafana
```

Configure Grafana


Add Data Source (http://localhost:3000/datasources)

1. Type is InfluxDB
2. Server is http://localhost:8086
3. Database is "example"
4. Click Test at bottom and it should work.

Example query

![Image of query](https://github.com/scottmsilver/home-connection-test/query.png)

Example dashboards

![Image of dashboard](https://github.com/scottmsilver/home-connection-test/dashboard_example.png)



Notes on iperf3

The stuff in repositories doesn't support auth.
So, important to build it yourself or downlaod and install.
If you build yourself, be sure to install openssl-dev first

To install and build iperf3:

```
sudo apt-get install libssl-dev
git clone https://github.com/esnet/iperf
cd iperf
make
sudo make install
```


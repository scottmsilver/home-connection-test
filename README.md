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

others: 

```
https://pip.pypa.io/en/stable/installing/
```

Install iperf

debian: 

sudo apt-get install iperf3

MacOS: 

brew install iperf

Install iperf for python

```
pip install iperf3
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
pip install speedtest-cli
```

Install influxdb 

https://docs.influxdata.com/influxdb/v1.8/introduction/install/
Be sure to start it running.

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

Now start collecting some data

```
python3 collect-data.py --iperf-server 35.224.53.38 --iperf-server-port 6201
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

Import dashboard (http://localhost:3000/dashboards)

1. Click Import
2. Click From JSON
3. Find the file "performance.json" 



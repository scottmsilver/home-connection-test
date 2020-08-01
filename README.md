
Ensure you have python3 installed

pi@raspberrypi:~ $ python3 -V
Python 3.7.3

It is ok to have python2 installed (typically it's the default python)

Ensure / Install pip3

pi@raspberrypi:~ $ pip3 -V
pip 18.1 from /usr/lib/python3/dist-packages/pip (python 3.7)

or

debian: sudo apt-get install python3-pip
others: https://pip.pypa.io/en/stable/installing/

Install iperf

debian: sudo apt-get install iperf3
MacOS: brew install iperf

Install iperf for python

pip install iperf3

Install speedtest

MacOS: brew install speedtest-cli
Debian: sudo apt-get install speedtest-cli.

Install speedtest python

pip install speedtest-cli

Install influxdb 

https://docs.influxdata.com/influxdb/v1.8/introduction/install/

Install grafana

Debian: Debian release https://grafana.com/docs/grafana/latest/installation/debian/
Mac: https://grafana.com/docs/grafana/latest/installation/mac/
Use brew instructions here

brew install grafana
brew services start grafana

Configure Grafana

Configure Home Connection Test


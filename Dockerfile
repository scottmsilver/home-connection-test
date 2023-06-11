FROM python:3-alpine

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN apk add bash curl iptables
COPY ndt7-client /usr/bin
COPY speedtest-mlab.py .
COPY udptest-telegraf.py .

# docker build -t ssilver/connection-test:1.0 .
# docker run --cap-add=NET_ADMIN ssilver/connection-test:1.0 python3 speedtest-mlab.py --desired_interface att --ndt7_binary=ndt7-client --dscp_class=AF12
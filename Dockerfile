FROM python:3-alpine

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN apk add bash curl iptables iperf3
COPY ndt7-client /usr/bin
COPY speedtest-mlab.py .
COPY udptest-telegraf.py .
COPY run-udptest.sh .

# docker build -t scottmsilver/connection-test:1.0 .
# docker push scottmsilver/connection-test:1.0 
# docker run --cap-add=NET_ADMIN ssilver/connection-test:1.0 python3 speedtest-mlab.py --desired_interface att --ndt7_binary=ndt7-client --dscp_class=AF12
# docker run -v /path/to/config-and-public.pem:/config scottmsilver/connection-test:1.0  bash run-udptest.sh --interface att --port 6204
# sudo usermod -aG docker telegraf
# newgrp docker
# now test with:
# become telegraf
# sudo -u telegraf bash
# telegraf -test
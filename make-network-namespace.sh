#!/bin/bash
#
# Create a network namespace netns that can route to the Internet with a specific DSCP class.
# The idea is that you would run some application in the namespace and then have the
# router look for the specific DSCP class and route it through a specific egress interface (from
# the complete network)
#
# For example, I Use AF13 and AF12 to route through Starlink and Comcast respectively.
#
# The basic idea is that the namespace will have a veth pair (base_network.1 and base_network.2), 
# one end of which is in the
# namespace and the other end of which is in the default namespace. The default namespace
# end of the veth pair will have a default route through <iface> that is connected to the Internet.
# The namespace will have a default route to the default namespace end of the veth pair.
#
# The script will also set up a resolv.conf file in the namespace that will allow DNS lookups.
#
# sudo ./make-network-namespace.sh  --netns starlink --iface enp2s0 --dscp_class AF13 --base_network 10.200.1 
# sudo ./make-network-namespace.sh  --netns comcast --iface enp2s0 --dscp_class AF12 --base_network 10.200.2 
# sudo ./make-network-namespace.sh  --netns att --iface enp2s0 --dscp_class AF11 --base_network 10.200.3
# To see what is happening on the main namespace, run:
# sudo tcpdump -i veth1 -v -n 

if [[ $EUID -ne 0 ]]; then
    echo "You must be root to run this script"
    exit 1
fi

# Initialize our own variables with default values:
NETNS="ns1"
BASE_NETWORK="10.200.1"
DSCP_CLASS="AF11"
IFACE=""

# A usage function
usage() {
  echo "Usage: $0 [--netns <netns>] [--base_network <base_network>] [--dscp_class <dscp_class>]" 1>&2;
  exit 1;
}

# parse arguments
while (( "$#" )); do
  case "$1" in
    --netns)
      if [ -n "$2" ] && [ ${2:0:1} != "-" ]; then
        NETNS=$2
        shift 2
      else
        echo "Error: Argument for $1 is missing" >&2
        exit 1
      fi
      ;;
    --iface)
      if [ -n "$2" ] && [ ${2:0:1} != "-" ]; then
        IFACE=$2
        shift 2
      else
        echo "Error: Argument for $1 is missing" >&2
        exit 1
      fi
      ;;
    --base_network)
      if [ -n "$2" ] && [ ${2:0:1} != "-" ]; then
        BASE_NETWORK=$2
        shift 2
      else
        echo "Error: Argument for $1 is missing" >&2
        exit 1
      fi
      ;;
    --dscp_class)
      if [ -n "$2" ] && [ ${2:0:1} != "-" ]; then
        DSCP_CLASS=$2
        shift 2
      else
        echo "Error: Argument for $1 is missing" >&2
        exit 1
      fi
      ;;
    -*|--*=) 
      echo "Error: Unsupported flag $1" >&2
      usage
      ;;
    *)
      echo "Error: Unsupported argument $1" >&2
      usage
      ;;
  esac
done

NS=${NETNS}
VETH="veth${NETNS}"
VPEER="vpeer${NETNS}"
VETH_ADDR="${BASE_NETWORK}.1"
VPEER_ADDR="${BASE_NETWORK}.2"
RESOLV_CONF_DIR="/etc/netns/${NS}"
RESOLV_CONF="${RESOLV_CONF_DIR}/resolv.conf"
DSCP="${DSCP_CLASS}"

# Continue with your script...
# dump all variables for debugging
echo "NS=${NS}, VETH=${VETH}, VPEER=${VPEER}, VETH_ADDR=${VETH_ADDR}, VPEER_ADDR=${VPEER_ADDR}, RESOLV_CONF_DIR=${RESOLV_CONF_DIR}, RESOLV_CONF=${RESOLV_CONF}, DSCP=${DSCP}"

# Remove namespace if it exists.
ip netns del $NS &>/dev/null
ip li delete ${VETH} 2>/dev/null
rm -rf ${RESOLV_CONF_DIR}

# Create a working resolv.conf for the namespace.
# The inherited one may not work because systems like Ubuntu set
# the resolver to some localhost 127.* address, thus DNS lookups
# cannot propagate from this namespace to the default namespace.
# I am using Google's resolver here.
mkdir -p ${RESOLV_CONF_DIR}
echo "nameserver 8.8.8.8" > ${RESOLV_CONF}

# Create namespace
ip netns add $NS

# Create veth link.
ip link add ${VETH} type veth peer name ${VPEER}

# Add peer to NS.
ip link set ${VPEER} netns $NS

# Setup IP address of ${VETH} (base_network.1)
ip addr add ${VETH_ADDR}/24 dev ${VETH}
ip link set ${VETH} up

# Setup IP ${VPEER} (base_network.2) inside the namespace
ip netns exec $NS ip addr add ${VPEER_ADDR}/24 dev ${VPEER}
ip netns exec $NS ip link set ${VPEER} up
ip netns exec $NS ip link set lo up
ip netns exec $NS ip route add default via ${VETH_ADDR}

# Enable IP-forwarding.
echo 1 > /proc/sys/net/ipv4/ip_forward

# Flush forward rules.
iptables -D FORWARD -i ${IFACE} -o ${VETH} -j ACCEPT
iptables -D FORWARD -o ${IFACE} -i ${VETH} -j ACCEPT
 
# Flush nat rules.
iptables -t nat -D POSTROUTING -s ${VPEER_ADDR}/24 -o ${IFACE} -j MASQUERADE

# Enable masquerading of VPEER_ADDR network to IFACE
iptables -t nat -A POSTROUTING -s ${VPEER_ADDR}/24 -o ${IFACE} -j MASQUERADE

iptables -A FORWARD -i ${IFACE} -o ${VETH} -j ACCEPT
iptables -A FORWARD -o ${IFACE} -i ${VETH} -j ACCEPT

# Set the DSCP value for all outbound traffic from this namespace.
if [ -n "$DSCP" ]; then
  ip netns exec ${NS} iptables -t mangle -A POSTROUTING -j DSCP --set-dscp-class ${DSCP}
fi

# Use a known website to find out our egress point.
IP=`ip netns exec ${NS} curl -s ipinfo.io/ip`
echo "Egress IP: ${IP}"

#ip netns exec ${NS} ip a

# Get into namespace
#ip netns exec ${NS} /bin/bash --rcfile <(echo "PS1=\"${NS}> \"")



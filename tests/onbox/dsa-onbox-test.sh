#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# velo5x0-bsp 0.4.0 / DSA kernel on-box checks (VBSP-REQ-3, VBSP-REQ-4).
# Run as root on the box, wrapped by velo5x0-evidence, e.g.
#   sudo velo5x0-evidence dsa-ports /srv/velo5x0-test/dsa-onbox-test.sh ports
# Phases:
#   ports   lan1..8 admin up (IPv6 RA and autoconf off first: link-local
#           only, no address or route from the LAN), carrier/map table,
#           link-local ping on lan1, 60 s isolation window on the switch
#           hardware counters, switch status
#   bridge  lan1+lan2 in a bridge with no address (loop-free: only LAN1 is
#           cabled; refuses if lan2 has carrier), offload evidence, removed
#   down    lan1..8 admin down
set -u
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
LANS="lan1 lan2 lan3 lan4 lan5 lan6 lan7 lan8"
ifname_of() { ip -o link show | awk -F': ' -v x="$1" '$1==x{sub(/@.*/,"",$2); print $2}'; }
table() {
	for i in $LANS; do
		b=/sys/class/net/$i
		echo "$i carrier=$(cat $b/carrier 2>/dev/null) oper=$(cat $b/operstate) speed=$(cat $b/speed 2>/dev/null) duplex=$(cat $b/duplex 2>/dev/null) phys_port_name=$(cat $b/phys_port_name 2>/dev/null) conduit=$(ifname_of "$(cat $b/iflink)")"
	done
}
snap() {
	for i in $LANS; do
		printf '%s ' "$i"
		ethtool -S "$i" | awk -F': ' '/^ *(in_good_octets|in_broadcasts|in_multicasts|in_unicast|out_octets|out_broadcasts|out_multicasts|out_unicast):/{gsub(/ /,"",$1); printf "%s=%s ", $1, $2}'
		echo
	done
}
case "${1:-}" in
ports)
	for i in $LANS; do
		echo 0 >/proc/sys/net/ipv6/conf/$i/accept_ra
		echo 0 >/proc/sys/net/ipv6/conf/$i/autoconf
		ip link set "$i" up
	done
	sleep 10
	echo "== table"; table
	echo "== addresses"; ip -br addr show | grep -E '^lan'
	echo "== link-local ping on lan1"; ping -6 -c 5 -W 2 -I lan1 ff02::1; echo "ping exit $?"
	echo "== isolation window (60 s): hardware counters"
	echo "T0"; snap
	sleep 60
	echo "T60"; snap
	echo "== switch status"; cat /sys/devices/platform/vc-edge5x0-dsa/status
	;;
bridge)
	[ "$(cat /sys/class/net/lan2/carrier 2>/dev/null)" = 1 ] && { echo "lan2 has carrier: refusing (loop risk)"; exit 1; }
	ip link add br-velotest type bridge stp_state 0
	echo 1 >/proc/sys/net/ipv6/conf/br-velotest/disable_ipv6
	ip link set lan1 master br-velotest
	ip link set lan2 master br-velotest
	ip link set br-velotest up
	sleep 8
	echo "== bridge"; ip -d link show br-velotest; bridge -d link show
	echo "== switch ids"; for i in lan1 lan2 lan5; do echo "$i $(ip -d link show $i | grep -o 'switchid [0-9a-f]*')"; done
	echo "== fdb (first 30)"; bridge fdb show br br-velotest | head -30
	echo "== switch status while bridged"; cat /sys/devices/platform/vc-edge5x0-dsa/status
	ip link set lan1 nomaster
	ip link set lan2 nomaster
	ip link del br-velotest
	sleep 3
	echo "== switch status after the bridge was removed"; cat /sys/devices/platform/vc-edge5x0-dsa/status
	;;
down)
	for i in $LANS; do ip link set "$i" down; done
	ip -br link
	;;
*) echo "usage: $0 ports|bridge|down"; exit 2 ;;
esac

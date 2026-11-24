#!/bin/bash
# Test sur un vrai noyau (root, veth + netns, modules bridge, sch_ingress, cls_matchall,
# act_mirred, nf_tables) des fonctions 1.0.3 : a lancer sur un switch ou une VM Debian :
# port security, err-disable, DHCP snooping, storm control, SPAN, IGMP snooping, export Prometheus.
SW=${1:-$(cd "$(dirname "$0")/.." && pwd)}; M=/tmp/st; rm -rf $M; mkdir -p $M
ok(){ echo "OK    - $1"; }; ko(){ echo "ECHEC - $1"; FAIL=1; }
export TLSWITCH_PORTS="ge1 ge2 ge3 ge4" TLSWITCH_RUN=$M/run TLSWITCH_ETC=$M/etc TLSWITCH_NO_DAEMON=1
T(){ python3 $SW/tlswitch/tlswitch.py "$@"; }
menage(){ for i in 1 2 3 4; do ip link del ge$i 2>/dev/null; ip netns del h$i 2>/dev/null; done
          ip link del br0 2>/dev/null; nft delete table bridge tlswitch 2>/dev/null; }
menage; trap menage EXIT
modprobe -a bridge sch_ingress cls_matchall act_mirred nf_tables 2>/dev/null
ip link add br-test type bridge 2>/dev/null && ip link del br-test || { echo "noyau sans bridge : test impossible ici"; exit 2; }
for i in 1 2 3 4; do
  ip netns add h$i; ip link add ge$i type veth peer name p$i; ip link set p$i netns h$i
  ip -n h$i link set p$i address 02:00:00:00:00:0$i; ip -n h$i addr add 192.0.2.$i/24 dev p$i
  ip -n h$i link set p$i up; ip -n h$i link set lo up
done
mkdir -p $M/etc
cat > $M/etc/startup.conf <<'C'
hostname SWITCH01
vlan 10
 name UTILISATEURS
ip dhcp snooping
ip igmp snooping querier
prometheus exporter
interface Gi1/0/1
 switchport access vlan 10
 ip dhcp snooping trust
interface Gi1/0/2
 switchport access vlan 10
 switchport port-security
 switchport port-security mac-address 0200.0000.0002
interface Gi1/0/3
 switchport access vlan 10
 storm-control broadcast level pps 20
interface Gi1/0/4
 description Sonde
monitor session 1 source interface Gi1/0/2 both
monitor session 1 destination interface Gi1/0/4
C
T apply $M/etc/startup.conf >/dev/null 2>$M/err; grep -v "systemctl\|hostname\|lldp\|mstp" $M/err
sleep 2
nft list table bridge tlswitch >/dev/null 2>&1 && ok "table nftables chargee" || ko "table nftables absente"
[ "$(cat /sys/class/net/br0/bridge/multicast_querier)" = 1 ] && ok "IGMP querier actif" || ko "IGMP querier"
ip link show ge4 | grep -q "master br0" && ko "destination SPAN encore dans le bridge" || ok "destination SPAN hors du bridge"

# port security : la MAC autorisee passe
ip netns exec h1 ping -c2 -W1 192.0.2.2 >/dev/null && ok "MAC autorisee : ping OK" || ko "MAC autorisee bloquee"
# SPAN : la sonde voit le trafic de Gi1/0/2
ip netns exec h4 timeout 4 tcpdump -ni p4 -c 2 icmp >$M/span 2>/dev/null &
sleep 1; ip netns exec h1 ping -c3 -W1 192.0.2.2 >/dev/null; wait
grep -q "192.0.2.2" $M/span && ok "SPAN : la sonde recoit la copie" || ko "SPAN : rien sur la sonde"
# port security : une autre MAC est rejetee puis le port passe err-disabled (violation shutdown)
T guard >/dev/null 2>&1
ip -n h2 link set p2 address 02:00:00:00:00:99
ip netns exec h2 ping -c2 -W1 192.0.2.1 >/dev/null && ko "MAC inconnue acceptee" || ok "MAC inconnue rejetee"
T guard >/dev/null 2>&1
grep -q ge2 $M/run/errdisable.json 2>/dev/null && ok "violation : Gi1/0/2 err-disabled" || ko "pas d'err-disable"
adm_up(){ ip -o link show "$1" | grep -q "[<,]UP[,>]"; }
adm_up ge2 && ko "Gi1/0/2 toujours actif" || ok "Gi1/0/2 coupe"
T guard >/dev/null 2>&1
adm_up ge2 && ko "le guard a rallume un port err-disabled" || ok "le guard laisse le port err-disabled"
printf 'clear errdisable interface Gi1/0/2\n' | T cli >/dev/null 2>&1
grep -q ge2 $M/run/errdisable.json && ko "clear errdisable" || ok "clear errdisable"

# DHCP snooping : une reponse de serveur DHCP venant d'un port non fiable est jetee
python3 - <<'P' && ok "reponses DHCP envoyees" || ko "envoi DHCP"
import subprocess
for h in ("h1", "h3"):
    subprocess.run(["ip", "netns", "exec", h, "python3", "-c",
      "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1);"
      "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('',67));[s.sendto(b'x'*300,('255.255.255.255',68)) for _ in range(5)]"])
P
c=$(nft -j list counter bridge tlswitch dhcp_ge3 | python3 -c "import json,sys;print([e['counter']['packets'] for e in json.load(sys.stdin)['nftables'] if 'counter' in e][0])")
[ "$c" -ge 5 ] && ok "DHCP pirate sur Gi1/0/3 jete ($c)" || ko "DHCP pirate non jete ($c)"
nft list table bridge tlswitch | grep -q dhcp_ge1 && ko "port trust filtre" || ok "port trust non filtre"

# storm control : rafale de broadcast limitee
ip netns exec h3 python3 -c "
import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
[s.sendto(b'y',('192.0.2.255',9)) for _ in range(500)]"
c=$(nft -j list counter bridge tlswitch stormb_ge3 | python3 -c "import json,sys;print([e['counter']['packets'] for e in json.load(sys.stdin)['nftables'] if 'counter' in e][0])")
[ "$c" -ge 400 ] && ok "storm control : $c broadcast jetes sur 500" || ko "storm control ($c jetes)"

# Prometheus
T metrics >/dev/null 2>&1 & P=$!; sleep 2
curl -s http://127.0.0.1:9101/metrics >$M/metrics
kill $P
grep -q 'tlswitch_storm_broadcast_drops_total{port="Gi1/0/3"' $M/metrics && grep -q 'tlswitch_port_rx_bytes_total{port="Gi1/0/1"' $M/metrics \
  && ok "export Prometheus ($(grep -vc '^#' $M/metrics) mesures)" || ko "export Prometheus"
[ -z "$FAIL" ] && echo "TOUS LES TESTS SECURITE OK" || { echo "DES TESTS ONT ECHOUE"; exit 1; }

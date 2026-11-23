#!/bin/bash
# Lab : 6 "ports" veth, des machines dans des namespaces, un routeur simule en LACP.
D=$(cd "$(dirname "$0")" && pwd); T=$D/../tlswitch/tlswitch.py
export TLSWITCH_PORTS="ge1 ge2 ge3 ge4 ge5 ge6" TLSWITCH_RUN=/tmp/lab/run TLSWITCH_ETC=/tmp/lab/etc TLSWITCH_NO_DAEMON=
mkdir -p /tmp/lab/etc; cp $D/lab.conf /tmp/lab/etc/startup.conf
ok(){ echo "OK   - $1"; }; ko(){ echo "ECHEC- $1"; FAIL=1; }
pingt(){ ip netns exec $1 ping -c2 -W1 -i0.3 $2 >/dev/null 2>&1; }
for n in routeur h3 h4 h5 h6; do ip netns add $n; done
for i in 1 2 3 4 5 6; do ip link add ge$i type veth peer name p$i; done
ip link set p1 netns routeur; ip link set p2 netns routeur
for i in 3 4 5 6; do ip link set p$i netns h$i; done
# routeur simule : LACP sur p1+p2, sous-interfaces VLAN
ip netns exec routeur sh -c 'ip link add bond0 type bond mode 802.3ad miimon 100 lacp_rate fast; ip link set p1 master bond0; ip link set p2 master bond0; ip link set bond0 up
 for v in 20 10 99; do ip link add link bond0 name bond0.$v type vlan id $v; ip addr add 192.168.$v.1/24 dev bond0.$v; ip link set bond0.$v up; done'
ip netns exec h3 sh -c 'ip addr add 192.168.20.3/24 dev p3; ip link set p3 up'
ip netns exec h4 sh -c 'ip addr add 192.168.20.4/24 dev p4; ip link set p4 up'
ip netns exec h5 sh -c 'ip addr add 192.168.20.5/24 dev p5; ip link set p5 up'   # meme sous-reseau mais VLAN 10 : doit etre isole
ip netns exec h6 sh -c 'ip link set p6 up; ip addr add 192.168.99.6/24 dev p6; ip link add link p6 name p6.20 type vlan id 20; ip addr add 192.168.20.6/24 dev p6.20; ip link set p6.20 up'
echo "== tlswitch boot"; python3 $T boot; sleep 4
echo "== tests"
pingt h3 192.168.20.4 && ok "access VLAN 20 : Gi1/0/3 -> Gi1/0/4" || ko "access VLAN 20"
pingt h3 192.168.20.5 && ko "VLAN 20 et 10 NE sont PAS isoles" || ok "isolation VLAN 20 / VLAN 10"
pingt routeur 192.168.20.3 && ok "trunk LACP Po1 -> VLAN 20 (Gi1/0/3)" || ko "trunk LACP VLAN 20"
pingt routeur 192.168.99.20 && ok "routeur -> SVI de gestion Vlan99" || ko "SVI via Po1"
pingt h6 192.168.99.20 && ok "native VLAN 99 non taguee sur Gi1/0/6 -> SVI" || ko "native VLAN"
pingt h6 192.168.20.3 && ok "VLAN 20 taguee sur Gi1/0/6" || ko "VLAN taguee sur trunk"
pingt routeur 192.168.20.6 && ok "trunk -> trunk VLAN 20" || ko "trunk -> trunk"
grep -q "Number of ports: 2" /proc/net/bonding/po1 && ok "LACP : 2 ports agreges dans Po1" || { ko "LACP"; cat /proc/net/bonding/po1 | head -40; }
echo "== idempotence (2e application)"
N=$(python3 -c "
import sys; sys.path.insert(0,'$D/../tlswitch'); import tlswitch as t, io
m,_=t.load_config('/tmp/lab/etc/startup.conf'); a=t.Applier(m, out=io.StringIO()); a.apply()
mods=[c for c in a.cmds if not any(k in c for k in (' up',' alias ',' guard ','type bridge','systemctl','lldpcli','ip route replace','hostnamectl'))]
print(len(mods), mods)")
echo "  commandes de modification lors d'une 2e application : $N"
echo "== CLI : passer Gi1/0/4 en VLAN 10 a chaud"
printf 'conf t\ninterface gi1/0/4\n switchport access vlan 10\nend\nshow vlan brief\nshow etherchannel summary\nshow mac address-table\nshow interfaces status\n' | python3 $T cli
pingt h3 192.168.20.4 && ko "Gi1/0/4 toujours en VLAN 20" || ok "Gi1/0/4 sorti de la VLAN 20"
pingt h4 192.168.20.5 && ok "Gi1/0/4 et Gi1/0/5 ensemble en VLAN 10" || ko "VLAN 10"
echo "== retour arriere : on casse l'acces de gestion depuis une session distante, sans confirmer"
printf 'conf t\ninterface Vlan99\n no ip address\n ip address 192.168.99.99 255.255.255.0\nend\n' | SSH_CONNECTION="192.168.99.6 5000 192.168.99.20 22" python3 -c "
import sys; sys.path.insert(0,'$D/../tlswitch'); import tlswitch as t
t.cli(['--remote'])" 2>&1 | grep -i "retour"
# raccourcit le delai a 5 s pour le test
python3 -c "import time;open('/tmp/lab/run/rollback.deadline','w').write(str(time.time()+5))"
pingt h6 192.168.99.99 && ok "nouvelle IP active (192.168.99.99)" || ko "nouvelle IP"
R=1; for i in $(seq 1 10); do sleep 3; pingt h6 192.168.99.20 && { R=0; break; }; done
[ $R = 0 ] && ok "RETOUR ARRIERE automatique : 192.168.99.20 revenue" || { ko "retour arriere"; ip -br addr show vlan99; }
echo "== panne d'un lien de l'agregat"
ip netns exec routeur ip link set p1 down; sleep 2
pingt routeur 192.168.20.3 && ok "Po1 continue sur un seul lien" || ko "basculement LACP"
ip netns exec routeur ip link set p1 up
echo "== guard : on supprime le bridge (simulation de crash)"
ip link del br0; python3 $T guard; sleep 4
pingt h3 192.168.20.4 >/dev/null; pingt routeur 192.168.20.3 && ok "guard a tout reconstruit" || ko "guard"
[ -z "$FAIL" ] && echo "TOUS LES TESTS SONT OK" || echo "IL Y A DES ECHECS"

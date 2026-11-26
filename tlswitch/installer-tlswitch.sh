#!/bin/bash
# =============================================================================
#  TL-SwitchOS - installateur (Debian 13 "trixie", x86_64)
#  Transforme la machine en switch administrable : CLI type Cisco (console
#  serie + SSH), interface web HTTPS, VLAN, LACP, RSTP, LLDP, SNMP.
#
#  Usage (en root, depuis le dossier du paquet) :
#     bash installer-tlswitch.sh
#  Variables optionnelles :
#     TLS_HOSTNAME=SWITCH01         nom du switch           (defaut : tl-switch)
#     TLS_ADMIN_PASS=...            mot de passe definitif du compte admin
#                                   (sinon admin / admin provisoire, a changer a la 1re connexion)
#     TLS_ADMIN_HASH='$6$...'       ou directement son hash
#     TLS_LOCK_ROOT=1               verrouille root (secours : start-shell depuis la CLI)
#     TLS_IP=192.168.1.30/24        IP de gestion fixe       (defaut : garde l'IP actuelle, sinon DHCP)
#     TLS_GW=192.168.1.1            passerelle
#     TLS_MGMT_VLAN=99              VLAN de l'IP de gestion  (defaut : 1)
#     TLS_MGMT=none                 port de gestion dedie : none | last | nom d'interface (defaut : none)
#     TLS_SERIAL=ttyS0 TLS_BAUD=115200   console serie
#     TLS_DNS=192.168.1.1           serveur DNS (defaut : celui de la machine)
#     TLS_VERBOSE=1                 affiche toute la sortie d'apt
# =============================================================================
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/tlswitch
ETC=/etc/tlswitch
HOSTNAME_NEW="${TLS_HOSTNAME:-tl-switch}"
MGMT_VLAN="${TLS_MGMT_VLAN:-1}"
SERIAL="${TLS_SERIAL:-ttyS0}"
BAUD="${TLS_BAUD:-115200}"
MGMT="${TLS_MGMT:-none}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "lance ce script en root"
[ -f "$SRC/tlswitch.py" ] || die "tlswitch.py introuvable a cote du script"
grep -qiE 'debian' /etc/os-release || die "Debian requis (12 ou 13)"

# -----------------------------------------------------------------------------
say "1/9 Paquets"
export DEBIAN_FRONTEND=noninteractive
# TLS_VERBOSE=1 : on montre la sortie d'apt (installation depuis l'ISO)
if [ "${TLS_VERBOSE:-0}" = 1 ]; then OUT=/dev/stdout; Q=""; else OUT=/dev/null; Q="-qq"; fi
APT_OPTS="-o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Dpkg::Use-Pty=0"
CORE="python3 iproute2 ethtool lldpd openssh-server openssl sudo ca-certificates"
# utiles mais pas bloquants (mstpd absent = STP classique a la place de RSTP)
OPT="console-setup keyboard-configuration kbd mstpd isc-dhcp-client snmpd rsyslog nftables libpam-radius-auth irqbalance lm-sensors traceroute iperf3 fail2ban systemd-timesyncd tcpdump less nano bridge-utils"
# firmwares des cartes reseau (le noyau Debian a deja tous les pilotes Ethernet ; certaines
# puces en ont besoin pour fonctionner) : Realtek, Broadcom, QLogic, Chelsio, Myricom,
# Netronome, Cavium, Intel. Installes meme sans la carte : on peut en ajouter une plus tard.
FW="firmware-realtek firmware-bnx2 firmware-bnx2x firmware-qlogic firmware-netxen firmware-myricom firmware-netronome firmware-cavium firmware-misc-nonfree firmware-intel-misc firmware-linux-free"
manque() { local m=""; for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || m="$m $p"; done; echo "$m"; }
MC=$(manque $CORE); MO=$(manque $OPT $FW)
if [ -z "$MC$MO" ]; then
  echo "   tous les paquets sont deja installes (installateur Debian) : rien a telecharger"
else
  echo "   a installer :$MC$MO"
  apt-get update $Q $APT_OPTS >$OUT 2>&1 || echo "   (mise a jour de la liste des paquets impossible : pas d'Internet ?)"
  echo "iperf3 iperf3/start_daemon boolean false" | debconf-set-selections 2>/dev/null || true
  if [ -n "$MC" ]; then
    # shellcheck disable=SC2086
    apt-get install -y $Q $APT_OPTS --no-install-recommends $MC >$OUT 2>&1 || die "paquets indispensables impossibles a installer :$MC (Internet ?)"
  fi
  if [ -n "$MO" ]; then
    # shellcheck disable=SC2086
    if ! apt-get install -y $Q $APT_OPTS --no-install-recommends $MO >$OUT 2>&1; then
      for p in $MO; do   # un paquet manquant ne doit pas bloquer les autres
        apt-get install -y $Q $APT_OPTS --no-install-recommends "$p" >$OUT 2>&1 || echo "   (paquet $p indisponible, ignore)"
      done
    fi
  fi
  echo "   paquets : OK"
fi
command -v dhclient >/dev/null || command -v dhcpcd >/dev/null || \
  apt-get install -y $Q --no-install-recommends dhcpcd-base >$OUT 2>&1 || true
# snmpd n'est active que si la config contient "snmp-server community"
systemctl disable --now snmpd >/dev/null 2>&1 || true
# NetworkManager / ifupdown ne doivent pas toucher aux ports du switch
systemctl disable --now NetworkManager >/dev/null 2>&1 || true

# -----------------------------------------------------------------------------
say "2/9 Logiciel TL-SwitchOS"
install -d -m 755 $LIB $LIB/web
install -m 755 "$SRC/tlswitch.py" $LIB/tlswitch.py
install -m 644 "$SRC/tlswitch_web.py" $LIB/tlswitch_web.py
cp -r "$SRC/web/." $LIB/web/
# tout le dossier est garde : il sert de base aux mises a jour et au retour arriere
for f in depot-apt.conf depot-apt.gpg maj-publique.pem; do [ -f "$SRC/$f" ] && install -m 644 "$SRC/$f" $LIB/; done
for f in maj.sh marque.sh installer-tlswitch.sh premier-demarrage.sh; do [ -f "$SRC/$f" ] && install -m 755 "$SRC/$f" $LIB/; done
[ -d "$SRC/boot" ] && { install -d $LIB/boot; cp -r "$SRC/boot/." $LIB/boot/; }
install -d -m 755 /var/lib/tlswitch/maj
# cle publique : seules les mises a jour signees par TA cle seront acceptees
if [ -f "$SRC/maj-publique.pem" ]; then install -d -m 700 $ETC; install -m 644 "$SRC/maj-publique.pem" $ETC/maj.pub; echo "   mises a jour : signature exigee"; fi
ln -sf $LIB/tlswitch.py /usr/local/sbin/tlswitch
cat > /usr/local/bin/tlsh <<'EOF'
#!/bin/sh
# Shell de connexion des comptes du switch : ouvre la CLI (en root via sudo, le niveau
# de privilege est verifie dans la CLI d'apres "username ... privilege").
if [ "$(id -u)" = 0 ]; then exec /usr/local/sbin/tlswitch cli "$@"; fi
exec sudo -n /usr/local/sbin/tlswitch cli
EOF
chmod 755 /usr/local/bin/tlsh
grep -q '^/usr/local/bin/tlsh$' /etc/shells || echo /usr/local/bin/tlsh >> /etc/shells
groupadd -f tlswitch
cat > /etc/sudoers.d/tlswitch <<'EOF'
Defaults:%tlswitch env_keep += "SSH_CONNECTION"
%tlswitch ALL=(root) NOPASSWD: /usr/local/sbin/tlswitch cli
EOF
chmod 440 /etc/sudoers.d/tlswitch
visudo -cf /etc/sudoers.d/tlswitch >/dev/null || die "sudoers invalide"

# -----------------------------------------------------------------------------
say "3/9 Ports reseau"
mapfile -t NICS < <(for d in /sys/class/net/*; do
  n=$(basename "$d")
  [ -e "$d/device" ] || continue                      # uniquement le materiel physique
  [ "$(cat "$d/type")" = 1 ] || continue              # ethernet
  [ -d "$d/wireless" ] && continue
  printf '%s %s %s\n' "$(readlink -f "$d/device" | sed 's#.*/##')" "$n" "$(cat "$d/address")"
done | sort | awk '{print $2" "$3}')
[ ${#NICS[@]} -ge 2 ] || die "il faut au moins 2 ports ethernet (trouves : ${#NICS[@]})"

MGMT_IF=""
if [ "$MGMT" = last ]; then MGMT_IF=$(echo "${NICS[-1]}" | cut -d' ' -f1)
elif [ "$MGMT" != none ]; then MGMT_IF="$MGMT"; fi

# Si la machine est deja un TL-SwitchOS (ge*), on garde l'ordre existant
rm -f /etc/systemd/network/10-tlswitch-*.link
install -d -m 700 $ETC
: > $ETC/ports
i=0
for entry in "${NICS[@]}"; do
  cur=${entry% *}; mac=${entry#* }
  if [ -n "$MGMT_IF" ] && [ "$cur" = "$MGMT_IF" ]; then new=mgmt0
  else i=$((i+1)); new="ge$i"; echo "$new" >> $ETC/ports; fi
  cat > /etc/systemd/network/10-tlswitch-$new.link <<EOF
[Match]
PermanentMACAddress=$mac
[Link]
Name=$new
EOF
  printf '   %-10s -> %-6s (%s)\n' "$cur" "$new" "$mac"
done
NPORTS=$i
echo "   $NPORTS ports de switch : Gi1/0/1 a Gi1/0/$NPORTS"

# IP actuelle (pour ne pas perdre la main si on installe en SSH)
CUR_IF=$(ip -4 route show default 2>/dev/null | awk '{print $5; exit}' || true)
CUR_IP=""
[ -n "$CUR_IF" ] && CUR_IP=$(ip -4 -o addr show dev "$CUR_IF" 2>/dev/null | awk '{print $4; exit}' || true)
CUR_GW=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}' || true)
IP="${TLS_IP:-$CUR_IP}"; GW="${TLS_GW:-$CUR_GW}"
if [ "$IP" = dhcp ]; then IP=""; GW=""; fi
# adresse illisible (ex. "/24") : DHCP plutot qu'un Vlan de gestion sans adresse
if [ -n "$IP" ] && ! printf '%s' "$IP" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$'; then
  echo "   adresse de gestion '$IP' invalide : DHCP a la place"; IP=""; GW=""
fi
[ -n "$GW" ] && ! printf '%s' "$GW" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' && GW=""

# ifupdown : on ne garde que lo (les ports sont geres par TL-SwitchOS)
if [ -f /etc/network/interfaces ] && ! grep -q TL-SwitchOS /etc/network/interfaces; then
  cp /etc/network/interfaces /etc/network/interfaces.avant-tlswitch
  printf '# TL-SwitchOS : les ports sont geres par "tlswitch" (voir show running-config)\nauto lo\niface lo inet loopback\n' > /etc/network/interfaces
  rm -rf /etc/network/interfaces.d/* 2>/dev/null || true
fi

# Port de gestion dedie : systemd-networkd, independant de la config du switch
rm -f /etc/systemd/network/20-tlswitch-mgmt0.network
if [ -n "$MGMT_IF" ]; then
  cat > /etc/systemd/network/20-tlswitch-mgmt0.network <<'EOF'
# Port de gestion dedie : DHCP + adresse fixe de secours 192.168.254.254/24
# (brancher un PC en 192.168.254.1/24 pour toujours joindre le switch)
[Match]
Name=mgmt0
[Network]
DHCP=ipv4
Address=192.168.254.254/24
LinkLocalAddressing=ipv6
[DHCPv4]
RouteMetric=500
EOF
  systemctl enable systemd-networkd >/dev/null 2>&1 || true
  echo "   port de gestion dedie : mgmt0 (DHCP + 192.168.254.254/24)"
fi

# -----------------------------------------------------------------------------
say "4/9 Configuration initiale"
if [ ! -f $ETC/startup.conf ]; then
  TEMP=0
  if [ -n "${TLS_ADMIN_HASH:-}" ]; then HASH="$TLS_ADMIN_HASH"
  elif [ -n "${TLS_ADMIN_PASS:-}" ]; then HASH=$(printf '%s\n' "$TLS_ADMIN_PASS" | openssl passwd -6 -stdin)
  else
    # mot de passe provisoire admin / admin, a changer obligatoirement a la premiere connexion
    HASH=$(printf 'admin\n' | openssl passwd -6 -stdin); TEMP=1
  fi
  {
    echo "hostname $HOSTNAME_NEW"
    echo "enable secret 5 $HASH"
    echo "username admin privilege 15 secret 5 $HASH"
    if [ "$TEMP" = 1 ]; then echo "username admin password-change-required"; fi
    echo "banner motd ^Acces reserve aux personnes autorisees^"
    if [ "$MGMT_VLAN" != 1 ]; then printf 'vlan %s\n name GESTION\n' "$MGMT_VLAN"; fi
    echo "spanning-tree mode rstp"
    echo "lldp run"
    for p in $(seq 1 $NPORTS); do
      printf 'interface GigabitEthernet1/0/%s\n switchport mode access\n' "$p"
      if [ "$MGMT_VLAN" != 1 ]; then printf ' switchport access vlan %s\n' "$MGMT_VLAN"; fi
    done
    echo "interface Vlan$MGMT_VLAN"
    echo " description Gestion"
    if [ -n "$IP" ]; then echo " ip address $IP"; else echo " ip address dhcp"; fi
    if [ -n "$GW" ] && [ -n "$IP" ]; then echo "ip default-gateway $GW"; fi
    if [ -n "${TLS_DNS:-}" ]; then echo "ip name-server $TLS_DNS"
    else
      for d in $(awk '/^nameserver/ && $2 !~ /^127\./ {print $2}' /etc/resolv.conf 2>/dev/null | head -2 || true); do echo "ip name-server $d"; done
    fi
    echo "ip http secure-server"
    echo "ip ssh server"
    echo "end"
  } > $ETC/startup.conf
  chmod 600 $ETC/startup.conf
  python3 $LIB/tlswitch.py check $ETC/startup.conf
else
  echo "   startup-config existante conservee"
fi

# -----------------------------------------------------------------------------
say "5/9 Certificat HTTPS"
# (certificat moderne : SAN, usages ; refait par maj.sh s'il est d'un ancien modele)
if [ ! -f $ETC/web.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$HOSTNAME_NEW/O=TECH-LINUX" \
    -addext "subjectAltName=DNS:$HOSTNAME_NEW,DNS:$HOSTNAME_NEW.local" \
    -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" -keyout $ETC/web.key -out $ETC/web.crt 2>/dev/null
  chmod 600 $ETC/web.key
fi

# -----------------------------------------------------------------------------
say "6/9 Services"
cat > /etc/systemd/system/tlswitch.service <<'EOF'
[Unit]
Description=TL-SwitchOS - application de la configuration du switch
DefaultDependencies=no
After=systemd-udevd.service systemd-udev-settle.service local-fs.target
Before=network-pre.target network.target ssh.service
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/tlswitch boot
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/tlswitch-web.service <<'EOF'
[Unit]
Description=TL-SwitchOS - interface web HTTPS
After=tlswitch.service
Wants=tlswitch.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/tlswitch/tlswitch_web.py
Restart=always
RestartSec=2
Nice=5

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/tlswitch-guard.service <<'EOF'
[Unit]
Description=TL-SwitchOS - surveillance
After=tlswitch.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tlswitch guard
EOF
cat > /etc/systemd/system/tlswitch-guard.timer <<'EOF'
[Unit]
Description=TL-SwitchOS - surveillance chaque minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s
[Install]
WantedBy=timers.target
EOF
install -d /etc/systemd/system/ssh.service.d /etc/systemd/system/lldpd.service.d /etc/systemd/system/mstpd.service.d
for s in ssh lldpd mstpd; do
  printf '[Service]\nRestart=always\nRestartSec=3\n' > /etc/systemd/system/$s.service.d/tlswitch.conf
done
systemctl daemon-reload 2>/dev/null || true
for u in tlswitch.service tlswitch-web.service tlswitch-guard.timer ssh lldpd mstpd irqbalance; do
  systemctl enable "$u" >/dev/null 2>&1 || echo "   (activation de $u impossible ici)"
done

# SSH : pas de root, sessions surveillees
# (reglages SSH ecrits par maj.sh, rejoue a chaque mise a jour)
: > /etc/motd

# integration commune avec les mises a jour (secours au demarrage, etc.)
[ -f "$SRC/maj.sh" ] && bash "$SRC/maj.sh"

# depot APT : enregistre le paquet tlswitch de CETTE version (les suivantes arrivent par
# "install update"). Depot injoignable : pas grave, ce sera fait a la premiere mise a jour.
if [ -f /etc/apt/sources.list.d/tlswitch.sources ]; then
  V=$(python3 $LIB/tlswitch.py version)
  if apt-get update -q >/dev/null 2>&1 && \
     DEBIAN_FRONTEND=noninteractive apt-get install -y -q "tlswitch=$V" >/dev/null 2>&1; then
    echo "   depot TL-SwitchOS : paquet tlswitch $V enregistre"
  else
    echo "   depot TL-SwitchOS : pas encore joignable (sera fait par 'install update')"
  fi
fi

# -----------------------------------------------------------------------------
say "7/9 Fiabilite et performances (noyau)"
cat > /etc/sysctl.d/90-tlswitch.conf <<'EOF'
# TL-SwitchOS
# console : seulement les erreurs graves du noyau (le reste va dans dmesg / journal)
kernel.printk = 3 4 1 3
kernel.panic = 10
kernel.panic_on_oops = 1
kernel.softlockup_panic = 1
vm.swappiness = 10
net.core.netdev_max_backlog = 16384
net.core.netdev_budget = 600
net.core.netdev_budget_usecs = 8000
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
EOF
# pas de pare-feu sur le pont (le pare-feu, c'est le routeur ou pare-feu)
printf 'blacklist br_netfilter\ninstall br_netfilter /bin/false\n' > /etc/modprobe.d/tlswitch.conf
printf 'bonding\n8021q\nbridge\n' > /etc/modules-load.d/tlswitch.conf
# chien de garde materiel : redemarre si le systeme se fige
install -d /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=30s\nRebootWatchdogSec=3min\n' > /etc/systemd/system.conf.d/tlswitch-watchdog.conf
# journal persistant mais borne
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' > /etc/systemd/journald.conf.d/tlswitch.conf

# -----------------------------------------------------------------------------
say "8/9 Console serie ($SERIAL, $BAUD bauds)"
if [ -f /etc/default/grub ]; then
  sed -i -E '/^GRUB_(TERMINAL|TERMINAL_INPUT|TERMINAL_OUTPUT|SERIAL_COMMAND|TIMEOUT|THEME|GFXMODE|DISTRIBUTOR|BACKGROUND)=/d' /etc/default/grub
  UNIT=${SERIAL#ttyS}
  cat >> /etc/default/grub <<EOF
GRUB_TIMEOUT=5
GRUB_DISTRIBUTOR="TL-SwitchOS"
GRUB_TERMINAL_INPUT="console serial"
GRUB_SERIAL_COMMAND="serial --unit=$UNIT --speed=$BAUD"
EOF
  # menu GRUB personnalise TL-SwitchOS (ecran) + console serie en parallele
  if [ -f "$SRC/boot/theme.txt" ]; then
    TH=/boot/grub/themes/tlswitch
    install -d $TH
    cp "$SRC"/boot/theme.txt "$SRC"/boot/grub-fond.png "$SRC"/boot/select_*.png "$SRC"/boot/*.pf2 $TH/
    cat >> /etc/default/grub <<EOF
GRUB_TERMINAL_OUTPUT="gfxterm serial"
GRUB_GFXMODE=1024x768,auto
GRUB_THEME="$TH/theme.txt"
EOF
    echo "   theme GRUB TL-SwitchOS installe"
  else
    echo 'GRUB_TERMINAL_OUTPUT="console serial"' >> /etc/default/grub
  fi
  # l'ecran en dernier = console principale : tout le demarrage s'affiche a l'ecran,
  # la console serie recoit aussi les messages du noyau et son invite de connexion
  CMD="console=$SERIAL,${BAUD}n8 console=tty0 net.ifnames=1 systemd.show_status=1"
  sed -i "s|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"$CMD\"|" /etc/default/grub
  update-grub >/dev/null 2>&1 || true
fi
systemctl enable serial-getty@$SERIAL.service >/dev/null 2>&1 || true
# les .link de renommage doivent etre presents des l'initramfs
update-initramfs -u >/dev/null 2>&1 || true

# -----------------------------------------------------------------------------
say "9/9 Compte admin"
id admin >/dev/null 2>&1 || useradd -m -s /usr/local/bin/tlsh -G tlswitch admin
usermod -s /usr/local/bin/tlsh -aG tlswitch admin
# mot de passe pose tout de suite (sans attendre le 1er demarrage) : la connexion
# admin/admin marche meme si l'application de la configuration echoue
H=$(sed -n 's/^username admin privilege [0-9]* secret 5 \(\S*\).*/\1/p' $ETC/startup.conf 2>/dev/null | head -n 1)
[ -n "$H" ] && echo "admin:$H" | chpasswd -e
ADMIN_OK=0
if grep -q '^admin:[$]' /etc/shadow; then echo "   mot de passe admin en place"; ADMIN_OK=1
else echo "   ATTENTION : mot de passe admin non pose (root reste deverrouille)"; fi
echo "$HOSTNAME_NEW" > /etc/hostname
if [ "${TLS_LOCK_ROOT:-0}" = 1 ] && [ "${ADMIN_OK:-0}" = 1 ]; then passwd -l root >/dev/null; echo "   root verrouille (secours : start-shell depuis la CLI niveau 15)"; fi

cat <<EOF

=====================================================================
 TL-SwitchOS installe. Redemarre maintenant :   reboot
---------------------------------------------------------------------
 Ports      : Gi1/0/1 a Gi1/0/$NPORTS (tous en VLAN $MGMT_VLAN au depart)
 Gestion    : interface Vlan$MGMT_VLAN ${IP:-en DHCP}
 SSH        : ssh admin@${IP%/*}
 Web        : https://${IP%/*}
 Console    : $SERIAL $BAUD 8N1 (cable console/serie)
 Compte     : admin / admin si aucun mot de passe fourni (a changer a la 1re connexion)
 Secours    : start-shell depuis la CLI niveau 15 (root interdit en SSH)
=====================================================================
EOF

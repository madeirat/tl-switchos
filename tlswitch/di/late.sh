#!/bin/sh
# TL-SwitchOS - fin de l'installation Debian : installe TOUT TL-SwitchOS dans le systeme
# (comme OMV), avec l'avancement affiche dans la barre de progression de l'installateur.
. /usr/share/debconf/confmodule
# fichiers TL-SwitchOS : sur la cle (ISO) ou telecharges (iPXE)
S=/cdrom/tlswitch; [ -d $S ] || S=/tmp/tlswitch
debconf-loadtemplate tlswitch $S/di/templates 2>/dev/null || true
step() { db_subst tlswitch/progress STEP "TL-SwitchOS : $1" 2>/dev/null; db_progress INFO tlswitch/progress 2>/dev/null || true; }

D=/target/opt/tlswitch-install
step "copie des fichiers"
mkdir -p $D
cp -r $S/. $D/
chmod +x $D/*.sh
[ -f /tmp/tlswitch.env ] && cat /tmp/tlswitch.env >> $D/iso.env
# adresse de gestion = celle configuree par l'etape reseau de Debian
NI=/target/etc/network/interfaces
if grep -qE '^iface [^ ]+ inet static' $NI 2>/dev/null; then
  # pas d'awk dans l'installateur : lecture ligne a ligne
  ADDR=""; MASK=""; GWY=""; f=0
  while read -r k v r; do
    case "$k" in
      iface) case "$v $r" in *" inet static"*) f=1 ;; *) [ $f = 1 ] && break ;; esac ;;
      address) [ $f = 1 ] && [ -z "$ADDR" ] && ADDR=$v ;;
      netmask) [ $f = 1 ] && [ -z "$MASK" ] && MASK=$v ;;
      gateway) [ $f = 1 ] && [ -z "$GWY" ] && GWY=$v ;;
    esac
  done < $NI
  case "$ADDR" in
    */*) ;;
    *) P=0; OIFS=$IFS; IFS=.; set -- ${MASK:-255.255.255.0}; IFS=$OIFS
       for o in "$@"; do
         while [ "$o" -gt 0 ]; do P=$((P + (o & 1))); o=$((o >> 1)); done; done
       ADDR="$ADDR/$P" ;;
  esac
  DNSS=$(sed -n 's/^nameserver[ \t]*\([0-9.]*\).*/\1/p' /etc/resolv.conf 2>/dev/null | head -n 1)
  { echo "TLS_IP='$ADDR'"; echo "TLS_GW='$GWY'"; echo "TLS_DNS='$DNSS'"; } >> $D/iso.env
  logger -t tlswitch "IP de gestion reprise de l'installateur : $ADDR gw $GWY dns $DNSS"
else
  echo "TLS_IP='dhcp'" >> $D/iso.env
  logger -t tlswitch "IP de gestion : DHCP (comme l'installateur)"
fi
if [ -s /tmp/tlswitch.pass ]; then
  cp /tmp/tlswitch.pass /target/root/.tlswitch.pass; chmod 600 /target/root/.tlswitch.pass
fi
rm -f /tmp/tlswitch.pass

# clavier AZERTY dans le systeme installe (console locale)
cat > /target/etc/default/keyboard <<'EOK'
XKBMODEL="pc105"
XKBLAYOUT="fr"
XKBVARIANT="latin9"
XKBOPTIONS=""
BACKSPACE="guess"
EOK
chroot /target setupcon --save-only >/dev/null 2>&1 || true

# depots : si l'etape miroir de Debian n'a rien ecrit (reseau configure par TL-SwitchOS),
# on met les depots officiels pour que les paquets du switch et les mises a jour arrivent
if ! grep -qsE '^deb (https?)://' /target/etc/apt/sources.list /target/etc/apt/sources.list.d/*.list 2>/dev/null && \
   ! grep -qs '^URIs: http' /target/etc/apt/sources.list.d/*.sources 2>/dev/null; then
  C=$(sed -n 's/^VERSION_CODENAME=//p' /target/etc/os-release /target/usr/lib/os-release* 2>/dev/null | head -1)
  C=${C:-trixie}
  cat > /target/etc/apt/sources.list <<EOA
deb http://deb.debian.org/debian $C main non-free-firmware
deb http://deb.debian.org/debian $C-updates main non-free-firmware
deb http://security.debian.org/debian-security $C-security main non-free-firmware
EOA
fi
[ -s /target/etc/resolv.conf ] || cp /etc/resolv.conf /target/etc/resolv.conf 2>/dev/null || true

# console du premier demarrage de secours (si l'installation ci-dessous echoue)
T=tty1; grep -q tlstty=serie /proc/cmdline && T=ttyS0
sed -i "s#^TTYPath=.*#TTYPath=/dev/$T#" $D/tlswitch-firstboot.service
cp $D/tlswitch-firstboot.service /target/etc/systemd/system/

# installation complete, etape par etape
cat > /target/tmp/tlswitch-late.sh <<'EOS'
#!/bin/bash
set -a; . /opt/tlswitch-install/iso.env; set +a
if [ -s /root/.tlswitch.pass ]; then TLS_ADMIN_PASS=$(cat /root/.tlswitch.pass); export TLS_ADMIN_PASS; fi
rm -f /root/.tlswitch.pass
export TLS_NONINTERACTIVE=1 TLS_VERBOSE=0
bash /opt/tlswitch-install/installer-tlswitch.sh
EOS
chmod +x /target/tmp/tlswitch-late.sh
# chroot direct (pas in-target) : apt ne doit pas parler a debconf pendant qu'on pilote la barre
for m in proc sys dev; do grep -q " /target/$m " /proc/mounts || mount --bind /$m /target/$m 2>/dev/null || true; done
env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive \
  chroot /target /bin/bash /tmp/tlswitch-late.sh > /target/var/log/tlswitch-install.log 2>&1 < /dev/null &
PID=$!
LAST=""; ESC=$(printf '\033')
while kill -0 $PID 2>/dev/null; do
  L=$(grep -aE '==>|: installation' /target/var/log/tlswitch-install.log 2>/dev/null | tail -1 | sed "s/$ESC\[[0-9;]*m//g; s/^==> //; s/^ *//")
  [ -n "$L" ] && [ "$L" != "$LAST" ] && { step "$L"; LAST="$L"; }
  sleep 1
done
wait $PID; RC=$?
rm -f /target/tmp/tlswitch-late.sh
if [ $RC -eq 0 ]; then
  step "termine"
  mkdir -p /target/etc/tlswitch && touch /target/etc/tlswitch/.installe
else
  # echec : l'installation sera reprise et AFFICHEE au premier demarrage
  step "reprise au premier demarrage"
  chroot /target systemctl enable tlswitch-firstboot.service || true
fi
exit 0

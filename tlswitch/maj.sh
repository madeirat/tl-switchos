#!/bin/bash
# =============================================================================
#  TL-SwitchOS - integration systeme, rejouee a chaque installation et a chaque
#  mise a jour (doit pouvoir tourner plusieurs fois sans rien casser).
#  Variables fournies par la mise a jour : TLS_ANCIENNE, TLS_NOUVELLE, TLS_SRC.
#  C'est ICI qu'une future version ajoute un paquet Debian, un service, etc.
# =============================================================================
set -u
LIB=/usr/local/lib/tlswitch
VAR=/var/lib/tlswitch
install -d -m 755 $VAR

# commande tlswitch (lien vers la version active)
ln -sf $LIB/tlswitch.py /usr/local/sbin/tlswitch

# SSH : pas de root, pas de redirection de ports/agent/X11 (un compte lecture ne doit pas
# pouvoir se servir du switch comme rebond vers les VLAN), sessions surveillees
if [ -d /etc/ssh/sshd_config.d ]; then
  cat > /etc/ssh/sshd_config.d/tlswitch.conf <<'EOS'
PermitRootLogin no
MaxAuthTries 3
LoginGraceTime 30
MaxStartups 10:30:60
ClientAliveInterval 30
ClientAliveCountMax 4
DisableForwarding yes
PermitTunnel no
PermitUserEnvironment no
PrintMotd no
PrintLastLog no
EOS
  if ! sshd -t 2>/dev/null; then
    # OpenSSH trop ancien pour DisableForwarding : equivalent en trois lignes
    sed -i '/^DisableForwarding/d' /etc/ssh/sshd_config.d/tlswitch.conf
    printf 'AllowTcpForwarding no\nX11Forwarding no\nAllowAgentForwarding no\n' >> /etc/ssh/sshd_config.d/tlswitch.conf
  fi
  sshd -t 2>/dev/null && systemctl reload ssh 2>/dev/null || true
fi

# certificat HTTPS d'un ancien modele (sans SAN, que Chrome refuse) : refait
E=/etc/tlswitch
if [ -f $E/web.crt ] && ! openssl x509 -in $E/web.crt -noout -ext subjectAltName 2>/dev/null | grep -q DNS; then
  H=$(hostname 2>/dev/null || echo tl-switch)
  if openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$H/O=TECH-LINUX" \
      -addext "subjectAltName=DNS:$H,DNS:$H.local" -addext "basicConstraints=critical,CA:FALSE" \
      -addext "keyUsage=critical,digitalSignature,keyEncipherment" -addext "extendedKeyUsage=serverAuth" \
      -keyout $E/web.key.new -out $E/web.crt.new 2>/dev/null; then
    chmod 600 $E/web.key.new; mv $E/web.key.new $E/web.key; mv $E/web.crt.new $E/web.crt
    systemctl try-restart tlswitch-web.service 2>/dev/null || true
  fi
fi

# signature des mises a jour : un switch installe sans la cle l'obtient ici ;
# les paquets suivants devront alors etre signes
for P in "${TLS_SRC:-/nonexistent}/maj-publique.pem" $LIB/maj-publique.pem; do
  if [ ! -f $E/maj.pub ] && [ -f "$P" ] && openssl pkey -pubin -in "$P" -noout 2>/dev/null; then
    install -d -m 700 $E; install -m 644 "$P" $E/maj.pub
    logger -t tlswitch "cle de signature des mises a jour installee"
  fi
done
[ -f "${TLS_SRC:-/nonexistent}/maj-publique.pem" ] && install -m 644 "$TLS_SRC/maj-publique.pem" $LIB/ 2>/dev/null

# historiques de la CLI : prives, sans les lignes portant un secret
for F in /root/.tlsh_history /home/*/.tlsh_history; do
  [ -f "$F" ] || continue
  chmod 600 "$F"
  sed -i -E '/(^|[^A-Za-z0-9_])([Ss][Ee][Cc][Rr][Ee][Tt]|[Pp][Aa][Ss][Ss][Ww]([Oo][Rr])?[Dd]|[Kk][Ee][Yy]|[Cc][Oo][Mm][Mm][Uu][Nn][Ii][Tt][Yy])([^A-Za-z0-9_]|$)|[Tt][Ee][Ss][Tt] +[Aa][Aa][Aa]|[Ss][Nn][Mm][Pp]-[Ss][Ee][Rr][Vv][Ee][Rr] +[Uu][Ss][Ee][Rr]/d' "$F"
done

# demarrage verrouille (pose par le mot de passe du compte admin) : apres une mise a
# jour de GRUB, les entrees Debian restent demarrables seules et le mode secours absent
if [ -f /etc/grub.d/01_tlswitch ] && [ -f /etc/grub.d/10_linux ]; then
  G=0
  if ! grep -q -- '--unrestricted' /etc/grub.d/10_linux; then
    sed -i -E 's/^(CLASS="--class gnu-linux[^"]*)"/\1 --unrestricted"/' /etc/grub.d/10_linux && G=1
  fi
  if ! grep -qE '^GRUB_DISABLE_RECOVERY="?true"?' /etc/default/grub 2>/dev/null; then
    sed -i '/^GRUB_DISABLE_RECOVERY=/d' /etc/default/grub; echo 'GRUB_DISABLE_RECOVERY="true"' >> /etc/default/grub; G=1
  fi
  grep -q password_pbkdf2 /boot/grub/grub.cfg 2>/dev/null || G=1
  [ $G = 1 ] && command -v update-grub >/dev/null && update-grub >/dev/null 2>&1
fi

# verrou du demarrage pose tout seul a la prochaine connexion d'un admin local
# (SSH ou console) : PAM transmet le mot de passe, deja verifie, a "tlswitch grub-auto"
cat > /usr/local/sbin/tlswitch-grub-auto <<'EOS'
#!/bin/sh
[ -f /etc/grub.d/01_tlswitch ] && exit 0
exec /usr/bin/python3 /usr/local/lib/tlswitch/tlswitch.py grub-auto
EOS
chmod 755 /usr/local/sbin/tlswitch-grub-auto
for P in /etc/pam.d/sshd /etc/pam.d/login; do
  [ -f "$P" ] && grep -q '^@include common-auth' "$P" || continue
  grep -q 'tlswitch-grub' "$P" && continue
  sed -i '/^@include common-auth/a auth optional pam_exec.so quiet expose_authtok /usr/local/sbin/tlswitch-grub-auto  # tlswitch-grub' "$P"
done

# firmwares des cartes reseau (meme liste que installer-tlswitch.sh) : installes en tache
# de fond 90 s plus tard, apt pouvant etre occupe par la mise a jour elle-meme
FW=$(sed -n 's/^FW="\(.*\)"$/\1/p' "${TLS_SRC:-$LIB}/installer-tlswitch.sh" $LIB/installer-tlswitch.sh 2>/dev/null | head -n 1)
M=""; for p in $FW; do dpkg -s "$p" >/dev/null 2>&1 || M="$M $p"; done
if [ -n "$M" ] && command -v systemd-run >/dev/null; then
  systemctl reset-failed tlswitch-firmwares.service 2>/dev/null
  systemd-run --quiet --unit tlswitch-firmwares --on-active=90 /bin/sh -c "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; for p in $M; do apt-get install -y -qq --no-install-recommends \$p || logger -t tlswitch \"firmware \$p indisponible (section non-free-firmware ?)\"; done" 2>/dev/null || true
fi

# secours : si la version active ne demarre pas, retour automatique (une fois)
# a la version precedente, puis redemarrage du switch avec elle.
cat > /usr/local/sbin/tlswitch-secours <<'EOS'
#!/bin/sh
LIB=/usr/local/lib/tlswitch; VAR=/var/lib/tlswitch
logger -t tlswitch "secours : la configuration n'a pas pu etre appliquee au demarrage"
[ -f $VAR/precedente/tlswitch.py ] || exit 0
[ -f $VAR/secours.fait ] && { logger -t tlswitch "secours : deja tente, on reste ainsi"; exit 0; }
date > $VAR/secours.fait
rm -rf $LIB.secours && cp -a $VAR/precedente $LIB.secours && rm -rf $LIB.casse && mv $LIB $LIB.casse && mv $LIB.secours $LIB
echo "$(date '+%Y-%m-%d %H:%M')  retour automatique a la version precedente (echec au demarrage)" >> /etc/tlswitch/historique-maj
logger -t tlswitch "secours : version precedente restauree, nouvel essai"
/usr/bin/python3 $LIB/tlswitch.py boot && systemctl restart tlswitch-web.service
EOS
chmod 755 /usr/local/sbin/tlswitch-secours
cat > /etc/systemd/system/tlswitch-secours.service <<'EOS'
[Unit]
Description=TL-SwitchOS - retour automatique a la version precedente
DefaultDependencies=no
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tlswitch-secours
EOS
install -d /etc/systemd/system/tlswitch.service.d
printf '[Unit]\nOnFailure=tlswitch-secours.service\n' > /etc/systemd/system/tlswitch.service.d/secours.conf

# l'invite de connexion attend que la configuration soit appliquee (IP + compte
# admin prets), sinon on voit un ecran sans adresse et admin/admin est refuse
for g in getty@.service serial-getty@.service; do
  install -d /etc/systemd/system/$g.d
  printf '[Unit]\nAfter=tlswitch.service\n' > /etc/systemd/system/$g.d/tlswitch.conf
done

# ecran de connexion : /etc/issue garde les sequences \4{vlanN} ; agetty y met
# lui-meme l'adresse IP et redessine l'ecran des qu'elle change (DHCP qui arrive
# apres le demarrage, changement en CLI ou web). Un /etc/issue sans sequence
# n'est jamais redessine : c'est pour ca qu'on ne remplace plus l'adresse en dur.
cat > /usr/local/sbin/tlswitch-ecran <<'EOS'
#!/bin/bash
M=/etc/tlswitch/issue.modele
ecrire() {
  [ -f $M ] || return 0
  cmp -s $M /etc/issue || { cp $M /etc/issue.tmp && mv /etc/issue.tmp /etc/issue; }
  agetty --reload 2>/dev/null || true
}
ecrire
[ "${1:-}" = --une-fois ] && exit 0
ip -4 monitor address 2>/dev/null | while read -r _; do sleep 1; ecrire; done
EOS
chmod 755 /usr/local/sbin/tlswitch-ecran
cat > /etc/systemd/system/tlswitch-ecran.service <<'EOS'
[Unit]
Description=TL-SwitchOS - adresses IP sur l'ecran de connexion
After=tlswitch.service
[Service]
ExecStart=/usr/local/sbin/tlswitch-ecran
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOS
systemctl enable tlswitch-ecran.service >/dev/null 2>&1 || true
systemctl restart tlswitch-ecran.service >/dev/null 2>&1 || true

# export Prometheus (active seulement par "prometheus exporter" dans la config)
cat > /etc/systemd/system/tlswitch-metrics.service <<'EOS'
[Unit]
Description=TL-SwitchOS - export Prometheus (compteurs des ports, STP, securite)
After=tlswitch.service
[Service]
ExecStart=/usr/local/sbin/tlswitch metrics
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOS

# le systeme s'affiche comme TL-SwitchOS (pas Debian)
[ -f "$LIB/marque.sh" ] && bash "$LIB/marque.sh" || true

# depot APT TL-SwitchOS (si la version a ete construite avec : depot-apt.conf + depot-apt.gpg)
if [ -s "$LIB/depot-apt.conf" ] && [ -s "$LIB/depot-apt.gpg" ]; then
  DEPOT_URL=$(sed -n 's/^DEPOT_URL=//p' "$LIB/depot-apt.conf" | tr -d "\"' " | head -n 1)
  if [ -n "$DEPOT_URL" ]; then
    install -d /usr/share/keyrings
    install -m 644 "$LIB/depot-apt.gpg" /usr/share/keyrings/tlswitch-depot.gpg
    cat > /etc/apt/sources.list.d/tlswitch.sources <<EOS
# Depot TL-SwitchOS : mises a jour du switch (show install updates / install update)
Types: deb
URIs: $DEPOT_URL
Suites: stable
Components: main
Signed-By: /usr/share/keyrings/tlswitch-depot.gpg
EOS
  fi
fi

# Paquet Debian necessaire a une nouvelle version : l'ajouter aux dependances du .deb
# (fabriquer-deb.sh), pas ici : pendant une mise a jour par le depot, apt est occupe.
systemctl daemon-reload 2>/dev/null || true
exit 0

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

#!/bin/bash
# =============================================================================
#  Serveur de construction et depots commun (LXC Debian 13), durci
#
#     bash installer-construction.sh            installe ou met a jour le serveur
#     construction ajouter tlswitchos            ajoute un projet (questions)
#
#  Pour chaque projet, il surveille GitHub, construit, signe, publie la Release,
#  puis met a jour le depot du projet : /srv/depot/<projet> (depot APT, ISO,
#  archive signee du site), servi en lecture seule par nginx :
#    - HTTPS 8443 pour le reverse proxy (https://deb.<domaine>/<projet>/) ;
#    - HTTP 80 pour les clients internes autorises (ex. le serveur web, qui y
#      recupere le site et verifie sa signature : installer-site-web.sh).
#
#  Chaque projet a son dossier /srv/construction/projets/<projet> (depot git,
#  cles, cache), sa configuration /etc/construction/<projet>.conf et sa minuterie
#  construction@<projet>.timer. La recette de chaque projet est un script de SON
#  depot git (variable RECETTE).
#
#  Securite :
#   - pare-feu nftables : SSH depuis le bastion, HTTPS 8443 depuis le proxy,
#     HTTP depuis les clients autorises ; sorties limitees a DNS, NTP, HTTP(S) ;
#   - SSH par cle uniquement, compte d'administration depuis le bastion, root refuse ;
#   - constructions lancees par l'utilisateur sans droits "construction", dans un
#     service systemd cloisonne (systeme en lecture seule, pas d'elevation) ;
#     nginx (www-data) ne peut que lire les depots, jamais les cles ;
#   - jetons GitHub lisibles par root et ce service seulement, jamais ecrits dans git ;
#   - nginx en lecture seule (GET/HEAD), sans liste des dossiers ni version ;
#   - mises a jour de securite Debian automatiques.
#
#  Reponses possibles par variables (sinon questions) :
#    BASTION="192.0.2.10"            IP du bastion
#    ADMIN_SSH="sshadmin"             compte SSH d'administration (cle posee par le bastion)
#    CLIENTS_HTTPS="192.0.2.2"       IP autorisees en HTTPS 8443 (reverse proxy)
#    CLIENTS_HTTP="192.0.2.55"       IP/reseaux autorises en HTTP 80 (vide = ferme)
# =============================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "a lancer en root"; exit 1; }
CONF_SERVEUR=/etc/construction/serveur.conf
[ -f "$CONF_SERVEUR" ] && . "$CONF_SERVEUR"

ip_ok() {  # IPv4 ou IPv4/masque, separees par des espaces ou des virgules
  local x o
  [ -n "${1// /}" ] || return 1
  for x in ${1//,/ }; do
    [[ "$x" =~ ^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})(/([0-9]{1,2}))?$ ]] || return 1
    for o in "${BASH_REMATCH[@]:1:4}"; do (( 10#$o <= 255 )) || return 1; done
    [ -z "${BASH_REMATCH[6]}" ] || (( 10#${BASH_REMATCH[6]} <= 32 )) || return 1
  done
}
virg() { echo "$*" | xargs | sed 's/ /, /g'; }

echo "==> Acces"
until ip_ok "${BASTION:-}"; do
  read -rp "IP (ou reseau) du bastion, seul autorise en SSH : " BASTION
done
if [ -z "${ADMIN_SSH:-}" ]; then
  read -rp "Compte SSH d'administration (sa cle est posee par le bastion) [sshadmin] : " ADMIN_SSH
  ADMIN_SSH=${ADMIN_SSH:-sshadmin}
fi
[[ "$ADMIN_SSH" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] && [ "$ADMIN_SSH" != root ] || { echo "ERREUR : compte invalide"; exit 1; }
until ip_ok "${CLIENTS_HTTPS:-}"; do
  read -rp "IP autorisees en HTTPS 8443 (reverse proxy) : " CLIENTS_HTTPS
done
if [ -z "${CLIENTS_HTTP+x}" ]; then
  read -rp "IP ou reseaux autorises en HTTP 80 (serveur web ; vide = ferme) : " CLIENTS_HTTP
fi
[ -z "$CLIENTS_HTTP" ] || ip_ok "$CLIENTS_HTTP" || { echo "ERREUR : IP HTTP invalides"; exit 1; }
BASTION=$(echo "${BASTION//,/ }" | xargs); CLIENTS_HTTPS=$(echo "${CLIENTS_HTTPS//,/ }" | xargs)
CLIENTS_HTTP=$(echo "${CLIENTS_HTTP//,/ }" | xargs)
install -d -m 750 /etc/construction
( umask 077; printf 'BASTION="%s"\nADMIN_SSH="%s"\nCLIENTS_HTTPS="%s"\nCLIENTS_HTTP="%s"\n' \
    "$BASTION" "$ADMIN_SSH" "$CLIENTS_HTTPS" "$CLIENTS_HTTP" > "$CONF_SERVEUR" )

echo "==> Paquets"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git gh gpg apt-utils dpkg-dev xorriso wget cpio openssl python3 \
  ca-certificates curl rsync nginx nftables openssh-server unattended-upgrades
rm -f /etc/nginx/sites-enabled/construction /etc/nginx/sites-available/construction   # ancienne version

echo "==> Utilisateur de construction (sans droits)"
id construction >/dev/null 2>&1 || useradd --system --home-dir /srv/construction \
  --shell /usr/sbin/nologin --user-group construction
install -d -m 755 -o root -g root /srv/construction /srv/depot
install -d -m 750 -o construction -g construction /srv/construction/projets
chown root:construction /etc/construction; chmod 750 /etc/construction
# /srv/construction/projets en 750 : nginx (www-data) ne voit ni les cles ni les jetons ;
# il ne lit que /srv/depot/<projet> (construction, 755)

echo "==> Commande construction"
cat > /usr/local/sbin/construction <<'EOS'
#!/bin/bash
# construction ajouter|lancer|journal|sauvegarder <projet> ; construction liste
set -euo pipefail
ACTION="${1:-liste}"; P="${2:-}"
B=/srv/construction/projets/$P; C=/etc/construction/$P.conf; W=/srv/depot/$P
nom_ok() { [[ "$P" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] && [ "$P" != serveur ] || { echo "nom de projet : minuscules, chiffres, tirets"; exit 1; }; }
root_ok() { [ "$(id -u)" = 0 ] || { echo "a lancer en root"; exit 1; }; }
# jeton GitHub donne a git par l'environnement : jamais ecrit dans .git/config
git_jeton() {
  export GIT_CONFIG_COUNT=2 GIT_TERMINAL_PROMPT=0
  export GIT_CONFIG_KEY_0=credential.https://github.com.helper GIT_CONFIG_VALUE_0=
  export GIT_CONFIG_KEY_1=credential.https://github.com.helper
  export GIT_CONFIG_VALUE_1='!f() { echo username=x-access-token; echo "password=$GH_TOKEN"; }; f'
}
# publication du dossier prepare vers le depot servi par nginx
publier() {  # <prepare> <depot>
  local src=$1 dst=$2
  [ -e "$src/dists/stable/InRelease" ] || return 0      # pas encore construit
  # 1) paquets et ISO, sans rien effacer ; 2) le tout, index en dernier, avec effacement :
  #    jamais d'index pointant vers un fichier absent
  rsync -rlt --chmod=D755,F644 --exclude /dists --exclude /site "$src/" "$dst/"
  rsync -rlt --chmod=D755,F644 --delete --delay-updates "$src/" "$dst/"
}
case "$ACTION" in
  ajouter)
    root_ok; nom_ok; cd /
    if [ ! -f "$C" ]; then
      read -rp "Depot GitHub (proprietaire/depot) : " REPO
      [[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "format : proprietaire/depot"; exit 1; }
      echo "Jeton : github.com > Settings > Developer settings > Fine-grained tokens,"
      echo "        acces a ce seul depot, permission 'Contents : Read and write', avec expiration."
      read -rsp "Jeton GitHub : " TOKEN; echo
      read -rp "Branche [main] : " BR; BR=${BR:-main}
      read -rp "Recette dans le depot [outils/publier-release.sh] : " RE; RE=${RE:-outils/publier-release.sh}
      case "$RE" in /*|*..*) echo "recette : chemin relatif dans le depot"; exit 1 ;; esac
      echo "Adresse publique du depot de ce projet (switchs et visiteurs du site),"
      read -rp "  ex. https://deb.exemple.fr/$P : " URL
      [[ "$URL" =~ ^https?://[A-Za-z0-9.:-]+(/[A-Za-z0-9._/-]*)?$ ]] || { echo "adresse invalide"; exit 1; }
      ( umask 027
        printf 'GH_REPO=%q\nGH_TOKEN=%q\nBRANCHE=%q\nRECETTE=%q\nDEPOT_URL=%q\nPUBLIER_PAGES=non\n' \
          "$REPO" "$TOKEN" "$BR" "$RE" "${URL%/}" > "$C" )
      chown root:construction "$C"; chmod 640 "$C"
    fi
    . "$C"
    GH_TOKEN=$GH_TOKEN gh api "repos/$GH_REPO" --jq .full_name >/dev/null \
      || { echo "ERREUR : depot $GH_REPO inaccessible avec ce jeton (corrige $C)"; exit 1; }
    install -d -m 750 -o construction -g construction "$B" "$B/cache" "$B/maison" "$B/publie"
    install -d -m 700 -o construction -g construction "$B/cles"
    install -d -m 755 -o construction -g construction "$W"
    export GH_TOKEN; git_jeton
    if [ -d "$B/depot/.git" ]; then
      runuser -u construction -- git -C "$B/depot" remote set-url origin "https://github.com/$GH_REPO.git"
    else
      runuser -u construction -- git clone -q --branch "$BRANCHE" "https://github.com/$GH_REPO.git" "$B/depot"
    fi
    systemctl enable --now "construction@$P.timer" >/dev/null
    echo "Projet $P pret. Premiere construction : construction lancer $P"
    echo "Cles creees a la premiere construction. Ensuite : construction sauvegarder $P"
    ;;
  lancer)
    nom_ok; [ -f "$C" ] || { echo "projet inconnu : $P"; exit 1; }
    if [ "$(id -u)" = 0 ]; then
      # toujours dans le service cloisonne, sous l'utilisateur construction
      journalctl -u "construction@$P" -f -n 0 -o cat & J=$!
      R=0; systemctl start "construction@$P" || R=$?
      sleep 1; kill $J 2>/dev/null || true
      [ $R = 0 ] && echo "== $P : termine" || echo "== $P : ECHEC (construction journal $P)"
      exit $R
    fi
    [ "$(id -un)" = construction ] || { echo "a lancer en root"; exit 1; }
    . "$C"
    exec 9>"$B/.verrou"; flock -n 9 || { echo "$P : construction deja en cours"; exit 0; }
    export GH_TOKEN; git_jeton
    cd "$B/depot"
    git fetch -q origin "$BRANCHE"
    git checkout -q -B "$BRANCHE" "origin/$BRANCHE"
    git reset -q --hard "origin/$BRANCHE"
    git clean -qfdx -e cles
    case "$RECETTE" in /*|*..*) echo "recette invalide"; exit 1 ;; esac
    mkdir -p "$B/publie"
    ( export DEPOT="$B/depot" CLES="$B/cles" CACHE="$B/cache" WWW="$B/publie" DEPOT_URL GH_REPO PUBLIER_PAGES
      bash "$B/depot/$RECETTE" )
    # a chaque passage : rattrape une publication ratee, sans effet si rien n'a change
    publier "$B/publie" "$W"
    ;;
  journal) nom_ok; exec journalctl -u "construction@$P" -n 200 -f ;;
  sauvegarder)
    root_ok; nom_ok; [ -d "$B/cles" ] || { echo "pas de cles pour $P"; exit 1; }
    F=/root/cles-$P-$(date +%F).tar.gz.gpg
    echo "Phrase de passe de la sauvegarde (12 caracteres mini, a garder a part) :"
    read -rsp "Phrase : " A; echo; read -rsp "Encore : " A2; echo
    [ "$A" = "$A2" ] && [ ${#A} -ge 12 ] || { echo "phrases differentes ou trop courtes"; exit 1; }
    tar czf - -C "$B" cles | gpg --batch --yes --pinentry-mode loopback --symmetric \
      --cipher-algo AES256 --passphrase-fd 3 -o "$F" 3< <(printf '%s' "$A")
    unset A A2; chmod 600 "$F"
    echo "Sauvegarde chiffree : $F"
    echo "Depuis Proxmox : pct pull <id> $F ./  -> range-la hors ligne, puis sur le LXC : rm $F"
    ;;
  liste)
    for f in /etc/construction/*.conf; do [ -e "$f" ] || continue; p=$(basename "$f" .conf)
      [ "$p" = serveur ] && continue
      printf '%-20s %s\n' "$p" "$(systemctl is-active "construction@$p.timer")"; done ;;
  *) echo "usage : construction ajouter|lancer|journal|sauvegarder <projet> ; construction liste"; exit 1 ;;
esac
EOS
chmod 755 /usr/local/sbin/construction

echo "==> Services"
cat > /etc/systemd/system/construction@.service <<'EOF'
[Unit]
Description=Construction et publication du projet %i
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/construction/%i.conf
[Service]
Type=oneshot
User=construction
Group=construction
ExecStart=/usr/local/sbin/construction lancer %i
TimeoutStartSec=2h
UMask=0022
Nice=10
Environment=HOME=/srv/construction/projets/%i/maison GH_NO_UPDATE_NOTIFIER=1 GH_PROMPT_DISABLED=1
# cloisonnement : seuls le dossier du projet et son depot sont modifiables
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/srv/construction/projets/%i /srv/depot/%i
ProtectHome=yes
PrivateTmp=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
RestrictRealtime=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=
EOF
cat > /etc/systemd/system/construction@.timer <<'EOF'
[Unit]
Description=Verifier les nouvelles versions de %i toutes les 10 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

echo "==> nginx (depots en lecture seule)"
D=/etc/ssl/depot; install -d -m 755 $D
if [ ! -s $D/cert.pem ]; then
  N=$(hostname -s)
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$N" \
    -addext "subjectAltName=DNS:$N,IP:$(hostname -I | awk '{print $1}')" -keyout $D/key.pem -out $D/cert.pem 2>/dev/null
  chmod 600 $D/key.pem
fi
ECOUTE80=""; [ -n "$CLIENTS_HTTP" ] && ECOUTE80="    listen 80 default_server;"
cat > /etc/nginx/sites-available/depot <<EOF
# depots : genere par installer-construction.sh (relance = ecrase)
server {
    listen 8443 ssl default_server;
$ECOUTE80
    server_name _;
    ssl_certificate     $D/cert.pem;
    ssl_certificate_key $D/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root /srv/depot;
    autoindex off;
    server_tokens off;
    index index.html;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;
    add_header Content-Security-Policy "default-src 'none'; frame-ancestors 'none'" always;
    client_max_body_size 1k;
    location ~ /\. { deny all; }
    location ~ \.iso\$ { add_header Content-Disposition attachment always; add_header X-Content-Type-Options nosniff always; }
    location / {
        limit_except GET { deny all; }
        try_files \$uri =404;
    }
}
EOF
ln -sf /etc/nginx/sites-available/depot /etc/nginx/sites-enabled/depot
rm -f /etc/nginx/sites-enabled/default
sed -i 's/^\s*#\?\s*server_tokens .*/\tserver_tokens off;/' /etc/nginx/nginx.conf
nginx -t -q && systemctl enable nginx >/dev/null 2>&1 && systemctl restart nginx

echo "==> SSH ($ADMIN_SSH par cle, depuis le bastion seulement)"
ALLOW=""; for b in $BASTION; do ALLOW="$ALLOW $ADMIN_SSH@$b"; done
cat > /etc/ssh/sshd_config.d/10-construction.conf <<EOF
# serveur de construction : cle uniquement, $ADMIN_SSH depuis le bastion seulement, root refuse
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
AllowUsers$ALLOW
X11Forwarding no
AllowTcpForwarding no
AllowAgentForwarding no
AllowStreamLocalForwarding no
PermitTunnel no
MaxAuthTries 3
LoginGraceTime 20
ClientAliveInterval 300
ClientAliveCountMax 2
LogLevel VERBOSE
EOF
sshd -t && systemctl enable ssh >/dev/null 2>&1 && systemctl restart ssh
id "$ADMIN_SSH" >/dev/null 2>&1 || echo "A FAIRE : le compte $ADMIN_SSH n'existe pas encore (relance le bloc du bastion pour ce LXC)"
for s in postfix rpcbind; do systemctl disable --now "$s" >/dev/null 2>&1 || true; done

echo "==> Pare-feu"
R80=""; [ -n "$CLIENTS_HTTP" ] && R80="tcp dport 80 ip saddr { $(virg $CLIENTS_HTTP) } accept"
cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
# serveur de construction : genere par installer-construction.sh (relance = ecrase)
flush ruleset
table inet filtre {
  chain entree {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state invalid drop
    ct state established,related accept
    tcp dport 22 ip saddr { $(virg $BASTION) } ct state new limit rate 10/minute accept
    tcp dport 8443 ip saddr { $(virg $CLIENTS_HTTPS) } accept
    $R80
    ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } limit rate 10/second accept
    ip6 nexthdr icmpv6 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, destination-unreachable, packet-too-big, time-exceeded } accept
    limit rate 5/minute log prefix "nft-in-refus " level info
  }
  chain transit {
    type filter hook forward priority 0; policy drop;
  }
  chain sortie {
    type filter hook output priority 0; policy drop;
    oif lo accept
    ct state invalid drop
    ct state established,related accept
    udp dport { 53, 67, 123, 547 } accept
    tcp dport { 53, 80, 443 } accept comment "DNS, GitHub, miroirs Debian"
    ip protocol icmp accept
    ip6 nexthdr icmpv6 accept
    limit rate 5/minute log prefix "nft-out-refus " level info
  }
}
EOF
if nft -c -f /etc/nftables.conf && nft -f /etc/nftables.conf; then
  systemctl enable nftables >/dev/null 2>&1 || true
  PAREFEU="actif (nftables)"
else
  PAREFEU="NON ACTIF : active le pare-feu Proxmox du conteneur (voir le guide)"
fi

echo "==> Mises a jour de securite automatiques"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# droits des projets deja presents (relance du script)
for f in /etc/construction/*.conf; do
  [ -e "$f" ] || continue; p=$(basename "$f" .conf); [ "$p" = serveur ] && continue
  chown root:construction "$f"; chmod 640 "$f"
  d=/srv/construction/projets/$p
  if [ -d "$d" ]; then
    install -d "$d/maison" "$d/publie"
    chown -R construction:construction "$d"; chmod 750 "$d"; chmod 700 "$d/cles" 2>/dev/null || true
  fi
  install -d -m 755 -o construction -g construction "/srv/depot/$p"
  if [ -d "$d/depot/.git" ]; then  # ancien clone avec le jeton dans l'URL : on l'enleve
    R=$(. "$f"; echo "$GH_REPO")
    (cd / && runuser -u construction -- git -C "$d/depot" remote set-url origin "https://github.com/$R.git")
  fi
done

IP=$(hostname -I | awk '{print $1}')
cat <<EOF

=====================================================================
 Serveur de construction et depots pret.  IP : $IP
---------------------------------------------------------------------
 Pare-feu :  $PAREFEU
 SSH :       $ADMIN_SSH par cle, depuis $BASTION (root refuse ; secours : pct enter)
 Depots :    https://$IP:8443/<projet>/ pour $CLIENTS_HTTPS
$([ -n "$CLIENTS_HTTP" ] && echo "             http://$IP/<projet>/ pour $CLIENTS_HTTP")
---------------------------------------------------------------------
 Ensuite :
   construction ajouter tlswitchos
   construction lancer tlswitchos
   construction sauvegarder tlswitchos
=====================================================================
EOF

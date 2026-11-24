#!/bin/bash
# =============================================================================
#  Site d'un projet sur un serveur web Apache existant (Debian), derriere un
#  reverse proxy. Le site est RECUPERE sur le serveur de construction et n'est publie que si
#  sa signature GPG (cle du depot du projet) est valide.
#
#     bash installer-site-web.sh            installe ou met a jour (relancable)
#
#  Ce que fait le script :
#   - compte systeme "site-depot", sans shell, proprietaire du seul dossier du site ;
#   - commande site-depot <projet> + minuterie toutes les 10 minutes : telecharge
#     site.tar.gz et sa signature, verifie avec gpgv et la cle epinglee a
#     l'installation, refuse les chemins dangereux et les liens, puis remplace
#     le contenu du site ;
#   - hote virtuel Apache <domaine>, reserve au reverse proxy, en-tetes de
#     securite (CSP stricte, HSTS...) ;
#   - ne touche NI au pare-feu NI a SSH de la machine : la recuperation passe par
#     une sortie HTTP (port 80) vers le serveur de construction.
#
#  Reponses possibles par variables (sinon questions) :
#    PROJET="tlswitchos"   DOMAINE="os.exemple.fr"
#    SOURCE="http://192.0.2.20/tlswitchos"   (serveur de construction, HTTP direct)
#    PROXY_IPS="192.0.2.2"                    (seules IP autorisees par Apache)
#    WEB_IP="192.0.2.55"                      (IP d'ecoute du vhost, defaut : *)
#    CLE_EMPREINTE="E30B..."                  (empreinte attendue de la cle : verifiee
#                                              sans question ; sinon, question)
# =============================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "a lancer en root"; exit 1; }
command -v apache2ctl >/dev/null || { echo "ERREUR : Apache absent (ce script ajoute un site a un Apache existant)"; exit 1; }

echo "==> Questions"
until [[ "${PROJET:-}" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]]; do read -rp "Nom du projet (ex. tlswitchos) : " PROJET; done
C=/etc/site-depot/$PROJET.conf
[ -f "$C" ] && . "$C"
until [[ "${DOMAINE:-}" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]]; do read -rp "Domaine du site (ex. os.exemple.fr) : " DOMAINE; done
until [[ "${SOURCE:-}" =~ ^https?://[A-Za-z0-9.:-]+/[a-z0-9][a-z0-9-]*$ ]]; do
  read -rp "Adresse du projet sur le serveur de construction (ex. http://192.0.2.20/$PROJET) : " SOURCE
done
until [[ "${PROXY_IPS:-}" =~ ^[0-9./\ ]+$ ]]; do read -rp "IP du reverse proxy (plusieurs : separees par des espaces) : " PROXY_IPS; done
WEB_IP=${WEB_IP:-*}
install -d -m 755 /etc/site-depot
( umask 022; printf 'PROJET=%q\nDOMAINE=%q\nSOURCE=%q\nPROXY_IPS=%q\nWEB_IP=%q\n' \
  "$PROJET" "$DOMAINE" "$SOURCE" "$PROXY_IPS" "$WEB_IP" > "$C" )

echo "==> Paquets"
export DEBIAN_FRONTEND=noninteractive
command -v gpgv >/dev/null && command -v curl >/dev/null && command -v rsync >/dev/null \
  && command -v gpg >/dev/null || { apt-get update -q; apt-get install -y -q gpg gpgv curl rsync; }

echo "==> Compte et dossier du site"
id site-depot >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin --user-group site-depot
DOC=/var/www/site-$PROJET
install -d -m 755 -o site-depot -g site-depot "$DOC" /var/lib/site-depot
install -d -m 755 -o site-depot -g site-depot "/var/lib/site-depot/$PROJET"

echo "==> Cle de signature du depot (epinglee)"
K=/etc/site-depot/$PROJET.gpg
if [ ! -s "$K" ]; then
  T=$(mktemp)
  curl -fsS --max-time 30 -o "$T" "$SOURCE/tlswitch-depot.gpg" 2>/dev/null \
    || curl -fsS --max-time 30 -o "$T" "$SOURCE/depot.gpg" \
    || { rm -f "$T"; echo "ERREUR : cle introuvable sur $SOURCE (premiere construction faite ?)"; exit 1; }
  FP=$(gpg --show-keys --with-colons "$T" 2>/dev/null | awk -F: '$1=="fpr" {print $10; exit}')
  if [ -n "${CLE_EMPREINTE:-}" ]; then
    [ "$FP" = "${CLE_EMPREINTE// /}" ] || { rm -f "$T"; echo "ERREUR : empreinte $FP differente de celle attendue"; exit 1; }
    echo "   empreinte verifiee : $FP"
  else
    echo "Empreinte de la cle recuperee : $FP"
    echo "Compare avec celle du serveur de construction :"
    echo "   gpg --homedir /srv/construction/projets/$PROJET/cles/gnupg --fingerprint"
    read -rp "Identiques ? [o/N] " R
    [ "$R" = o ] || { rm -f "$T"; echo "abandon"; exit 1; }
  fi
  install -m 644 "$T" "$K"; rm -f "$T"
fi

echo "==> Commande site-depot"
cat > /usr/local/sbin/site-depot <<'EOS'
#!/bin/bash
# site-depot <projet> : recupere le site signe sur le serveur de construction et le publie
set -euo pipefail
P="${1:?projet}"; [[ "$P" =~ ^[a-z0-9][a-z0-9-]{0,40}$ ]] || exit 1
. "/etc/site-depot/$P.conf"
K=/etc/site-depot/$P.gpg; DOC=/var/www/site-$P; E=/var/lib/site-depot/$P
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
curl -fsS --max-time 120 --max-filesize 209715200 -o "$T/site.tar.gz" "$SOURCE/site/site.tar.gz"
curl -fsS --max-time 30 --max-filesize 65536 -o "$T/site.tar.gz.sig" "$SOURCE/site/site.tar.gz.sig"
N=$(sha256sum "$T/site.tar.gz" | cut -d' ' -f1)
[ "$N" != "$(cat "$E/empreinte" 2>/dev/null)" ] || exit 0          # rien de nouveau
gpgv --keyring "$K" "$T/site.tar.gz.sig" "$T/site.tar.gz" 2>"$T/gpgv.log" \
  || { echo "SIGNATURE INVALIDE : site NON publie"; cat "$T/gpgv.log"; exit 1; }
# seulement des fichiers et dossiers ordinaires, aucun chemin absolu ni ..
L=$(tar -tvzf "$T/site.tar.gz")
echo "$L" | awk '{print substr($1,1,1)}' | grep -qv '^[-d]$' && { echo "archive refusee : liens ou fichiers speciaux"; exit 1; }
tar -tzf "$T/site.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))' && { echo "archive refusee : chemin dangereux"; exit 1; }
mkdir "$T/x"
tar -xzf "$T/site.tar.gz" -C "$T/x" --no-same-owner --no-same-permissions
[ -f "$T/x/index.html" ] || { echo "archive refusee : pas de index.html"; exit 1; }
# referencement : robots.txt et sitemap.xml avec le vrai domaine du site
J=$(date -u +%F)
printf 'User-agent: *\nAllow: /\nSitemap: https://%s/sitemap.xml\n' "$DOMAINE" > "$T/x/robots.txt"
cat > "$T/x/sitemap.xml" <<EOX
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://$DOMAINE/</loc>
    <lastmod>$J</lastmod>
    <changefreq>monthly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>
EOX
rsync -rt --delete --delay-updates --chmod=D755,F644 "$T/x/" "$DOC/"
echo "$N" > "$E/empreinte"
echo "site $P publie ($(date '+%F %T'))"
EOS
chmod 755 /usr/local/sbin/site-depot

cat > /etc/systemd/system/site-depot@.service <<'EOF'
[Unit]
Description=Recuperation du site signe %i sur le serveur de construction
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=site-depot
Group=site-depot
ExecStart=/usr/local/sbin/site-depot %i
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/www/site-%i /var/lib/site-depot/%i
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
EOF
cat > /etc/systemd/system/site-depot@.timer <<'EOF'
[Unit]
Description=Mettre a jour le site %i toutes les 10 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload

echo "==> Apache : hote virtuel $DOMAINE"
a2enmod -q headers expires >/dev/null
EXPR=$(for ip in $PROXY_IPS; do printf "%%{CONN_REMOTE_ADDR} -ipmatch '%s' || " "$ip"; done | sed 's/ || $//')
cat > "/etc/apache2/sites-available/site-$PROJET.conf" <<EOF
# Site $PROJET (genere par installer-site-web.sh, relance = ecrase)
<VirtualHost $WEB_IP:80>
    ServerName $DOMAINE
    DocumentRoot $DOC
    <Directory $DOC>
        Options -Indexes -FollowSymLinks -ExecCGI
        AllowOverride None
        <RequireAll>
            Require expr "$EXPR"
            Require method GET HEAD
        </RequireAll>
    </Directory>
    <FilesMatch "\.(php|phtml|cgi|pl|py|sh)$">
        Require all denied
    </FilesMatch>
    Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains"
    Header always set Content-Security-Policy "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    Header always set X-Content-Type-Options "nosniff"
    Header always set X-Frame-Options "DENY"
    Header always set Referrer-Policy "strict-origin-when-cross-origin"
    Header always set Permissions-Policy "camera=(), microphone=(), geolocation=(), usb=()"
    Header always set Cross-Origin-Opener-Policy "same-origin"
    ExpiresActive On
    ExpiresByType text/css "access plus 1 day"
    ExpiresByType text/javascript "access plus 1 day"
    ExpiresByType image/png "access plus 1 week"
    ExpiresByType image/svg+xml "access plus 1 week"
    AddDefaultCharset utf-8
    ErrorLog \${APACHE_LOG_DIR}/site-$PROJET-erreurs.log
    CustomLog \${APACHE_LOG_DIR}/site-$PROJET.log combined
</VirtualHost>
EOF
a2ensite -q "site-$PROJET" >/dev/null
apache2ctl configtest
systemctl reload apache2

echo "==> Premiere recuperation"
systemctl enable --now "site-depot@$PROJET.timer" >/dev/null
systemctl start "site-depot@$PROJET.service" || echo "A VOIR : journalctl -u site-depot@$PROJET (site pas encore construit ?)"
[ -f "$DOC/index.html" ] && echo "site en place : $DOC" || true

cat <<EOF

=====================================================================
 Site $PROJET pret : $DOMAINE -> $DOC
---------------------------------------------------------------------
 Source :   $SOURCE/site/site.tar.gz (signature verifiee avec $K)
 Mise a jour : toutes les 10 minutes, ou : systemctl start site-depot@$PROJET
 Journal :  journalctl -u site-depot@$PROJET
 Apache :   reserve a $PROXY_IPS
=====================================================================
EOF

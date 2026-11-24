#!/bin/bash
# =============================================================================
#  TL-SwitchOS - fabrique le paquet Debian "tlswitch" pour le depot APT
#
#     bash fabriquer-deb.sh TL-SwitchOS-1.0.1.tlsu
#
#  Resultat : tlswitch_1.0.1_all.deb
#
#  Le .deb transporte le paquet de mise a jour signe (.tlsu). A l'installation
#  par apt, il le passe au moteur de mise a jour du switch, qui garde tout son
#  filet de securite : verification de la signature, test du code et de la
#  configuration, sauvegarde, retour automatique a l'ancienne version si la
#  nouvelle ne tourne pas. Le switch redemarre ensuite ("install update" en CLI
#  ou bouton web), et au demarrage le secours remet l'ancienne version si besoin.
#
#  Seules les mises a jour "fonctionnalites" passent par le depot : une mise a
#  jour majeure (montee de version de Debian) reste un .tlsu a installer a la main.
#  Un paquet Debian necessaire a une nouvelle version s'ajoute dans DEPENDS
#  ci-dessous (et non dans maj.sh : apt est deja occupe pendant l'installation).
# =============================================================================
set -euo pipefail
TLSU="${1:?usage : bash fabriquer-deb.sh TL-SwitchOS-<version>.tlsu}"
[ -f "$TLSU" ] || { echo "fichier introuvable : $TLSU"; exit 1; }
DEPENDS="python3, iproute2, openssl, bridge-utils, ethtool, lldpd, openssh-server, openssh-client, sudo, ca-certificates, nftables, rsyslog, libpam-radius-auth | libpam-runtime"

W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
tar xf "$TLSU" -C "$W" MANIFEST.json
read -r VERSION TYPE < <(python3 -c "import json,sys;m=json.load(open(sys.argv[1]));print(m['version'], m.get('type','fonctionnalites'))" "$W/MANIFEST.json")
[ "$TYPE" = majeure ] && { echo "mise a jour majeure : a distribuer en .tlsu, pas par le depot APT"; exit 1; }

R="$W/racine"
install -d "$R/DEBIAN" "$R/usr/share/tlswitch" "$R/usr/share/doc/tlswitch"
cp "$TLSU" "$R/usr/share/tlswitch/TL-SwitchOS-$VERSION.tlsu"
NOTES=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('notes',''))" "$W/MANIFEST.json")
printf 'TL-SwitchOS %s\n\n%s\n' "$VERSION" "$NOTES" > "$R/usr/share/doc/tlswitch/LISEZMOI"

cat > "$R/DEBIAN/control" <<EOF
Package: tlswitch
Version: $VERSION
Architecture: all
Maintainer: TL-SwitchOS <tlswitch@localhost>
Depends: $DEPENDS
Section: net
Priority: optional
Description: TL-SwitchOS - systeme de switch de niveau 2
 Moteur, CLI et interface web de TL-SwitchOS. L'installation passe par le
 moteur de mise a jour du switch (signature, test, retour automatique).
EOF

cat > "$R/DEBIAN/postinst" <<'EOF'
#!/bin/sh
# TL-SwitchOS : installe la version transportee par le paquet via le moteur de
# mise a jour (jamais d'echec dpkg : un echec laisse l'ancienne version active
# et apt reste utilisable pour les mises a jour de securite Debian).
set -u
[ "${1:-}" = configure ] || exit 0
V=__VERSION__
P=/usr/share/tlswitch/TL-SwitchOS-$V.tlsu
# chemin absolu : le PATH de dpkg ne contient pas toujours /usr/local/sbin
T=/usr/local/sbin/tlswitch
[ -x "$T" ] || T=/usr/local/lib/tlswitch/tlswitch.py
if [ ! -x "$T" ]; then
  echo "tlswitch : TL-SwitchOS n'est pas installe sur cette machine (installer depuis l'ISO)."
  exit 0
fi
ACTUELLE=$("$T" version 2>/dev/null || echo 0)
if dpkg --compare-versions "$ACTUELLE" ge "$V"; then
  exit 0                                    # deja a jour (installation depuis l'ISO)
fi
echo "TL-SwitchOS $ACTUELLE -> $V"
if TLSWITCH_NO_REBOOT=1 "$T" upgrade "$P"; then
  rm -f /var/lib/tlswitch/apt-echec
  touch /run/reboot-required
  echo "tlswitch" >> /run/reboot-required.pkgs
  echo "TL-SwitchOS $V installe : redemarrage necessaire."
else
  echo "ATTENTION : TL-SwitchOS $V n'a pas pu etre installe, la version $ACTUELLE reste active."
  echo "Detail : show install summary (CLI) ou page Mise a jour (web)"
  mkdir -p /var/lib/tlswitch && echo "$V" > /var/lib/tlswitch/apt-echec
fi
exit 0
EOF
sed -i "s/__VERSION__/$VERSION/" "$R/DEBIAN/postinst"
chmod 755 "$R/DEBIAN/postinst"
OUT="tlswitch_${VERSION}_all.deb"
dpkg-deb --root-owner-group -Zxz --build "$R" "$OUT" >/dev/null
echo "Paquet Debian pret : $OUT"

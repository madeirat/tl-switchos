#!/bin/bash
# =============================================================================
#  TL-SwitchOS - mise a niveau de Debian (paquet de type "majeure").
#  Lance par la mise a jour APRES l'installation de la nouvelle version de
#  TL-SwitchOS, et AVANT le redemarrage. TLS_CODENAME = nom de la version cible
#  (ex : forky pour Debian 14). Sans surprise : on suit la methode officielle
#  Debian (systeme a jour, changement des depots, upgrade minimal, full-upgrade).
# =============================================================================
set -eo pipefail
NEW="${TLS_CODENAME:?TLS_CODENAME manquant}"
# shellcheck disable=SC1091
OLD=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "Debian actuel : $OLD, cible : $NEW"
if [ "$OLD" = "$NEW" ]; then echo "Deja en $NEW, rien a faire."; exit 0; fi

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a APT_LISTCHANGES_FRONTEND=none
APT=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

echo "1/5 Mise a jour complete de Debian $OLD"
apt-get update -q
apt-get "${APT[@]}" full-upgrade

echo "2/5 Depots : $OLD -> $NEW"
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  [ -f "$f" ] || continue
  cp -n "$f" "$f.avant-$NEW" 2>/dev/null || true
  sed -i -E "s/\b$OLD(-security|-updates|-backports)?\b/$NEW\1/g" "$f"
done
apt-get update -q

echo "3/5 Mise a niveau minimale"
apt-get "${APT[@]}" upgrade --without-new-pkgs

echo "4/5 Mise a niveau complete (la plus longue)"
apt-get "${APT[@]}" full-upgrade

echo "5/5 Nettoyage et noyau"
apt-get "${APT[@]}" autoremove --purge || true
apt-get clean
update-initramfs -u -k all >/dev/null 2>&1 || true
update-grub >/dev/null 2>&1 || true
# les composants du switch doivent toujours etre la
for p in python3 openssh-server openssl sudo ethtool lldpd; do
  dpkg -s "$p" >/dev/null 2>&1 || apt-get "${APT[@]}" install --no-install-recommends "$p" || true
done
bash /usr/local/lib/tlswitch/marque.sh 2>/dev/null || true
echo "Debian $NEW installe. Redemarrage pour terminer."

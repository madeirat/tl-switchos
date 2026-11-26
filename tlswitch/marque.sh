#!/bin/bash
# TL-SwitchOS - le systeme s'affiche comme TL-SwitchOS et non comme Debian
# (os-release, demarrage systemd, SSH, issue.net, motd). Idempotent : relance par
# maj.sh a chaque mise a jour et par os-upgrade.sh apres une mise a niveau Debian.
set -u
LIB=${TLSWITCH_LIB:-/usr/local/lib/tlswitch}
V=$(python3 "$LIB/tlswitch.py" version 2>/dev/null || echo "")
ORIG=/usr/lib/os-release.debian
# le fichier de Debian est deplace (dpkg-divert) : une mise a jour de base-files ne l'ecrase plus
if ! dpkg-divert --list /usr/lib/os-release 2>/dev/null | grep -q tlswitch; then
  cp -p /usr/lib/os-release "$ORIG"   # copie d'abord : le fichier ne manque jamais
  dpkg-divert --package tlswitch --no-rename --divert "$ORIG" --add /usr/lib/os-release >/dev/null
fi
[ -f "$ORIG" ] || exit 0
# shellcheck disable=SC1090
VID=$(. "$ORIG"; echo "${VERSION_ID:-}"); COD=$(. "$ORIG"; echo "${VERSION_CODENAME:-}")
cat > /usr/lib/os-release.tmp <<EOS
PRETTY_NAME="TL-SwitchOS${V:+ $V}"
NAME="TL-SwitchOS"
VERSION="${V}"
VERSION_ID="${V}"
ID=tlswitchos
ID_LIKE=debian
VERSION_CODENAME=${COD}
DEBIAN_VERSION_ID="${VID}"
HOME_URL="https://github.com/tech-linux-fr/tl-switchos"
ANSI_COLOR="1;36"
EOS
mv /usr/lib/os-release.tmp /usr/lib/os-release
ln -sf ../usr/lib/os-release /etc/os-release
echo "TL-SwitchOS${V:+ $V}" > /etc/issue.net
# ecran de connexion : jamais "Debian" (tlswitch le complete ensuite avec les adresses)
if ! grep -q TL-SwitchOS /etc/issue 2>/dev/null; then
  printf '\nTL-SwitchOS%s - \\n - console \\l\n\n' "${V:+ $V}" > /etc/issue
fi
: > /etc/motd
chmod -x /etc/update-motd.d/* 2>/dev/null || true
# SSH : ne pas annoncer "Debian" dans la banniere de version
if [ -d /etc/ssh/sshd_config.d ]; then
  echo "DebianBanner no" > /etc/ssh/sshd_config.d/tlswitch-marque.conf
  sshd -t 2>/dev/null || rm -f /etc/ssh/sshd_config.d/tlswitch-marque.conf
fi
exit 0

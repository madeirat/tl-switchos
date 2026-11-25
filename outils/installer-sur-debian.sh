#!/bin/bash
# =============================================================================
#  TL-SwitchOS - installation sur une Debian 13 deja installee (sans l'ISO)
#
#     curl -fsSLO __DEPOT_URL__/installer.sh && bash installer.sh
#
#  Prend la derniere version dans le depot APT signe (cle verifiee par son
#  empreinte), pose les questions du switch puis lance l'installateur, le meme
#  que celui de l'ISO. Le switch reste ensuite branche sur le depot : les
#  versions suivantes arrivent par "install update" (CLI) ou la page web.
#
#  Questions evitees avec des variables (voir installer-tlswitch.sh) :
#     TLS_HOSTNAME=SWITCH01 TLS_MGMT_VLAN=99 TLS_IP=192.0.2.10/24 TLS_GW=192.0.2.1
#     TLS_AUTO=1 : aucune question (valeurs par defaut ou variables)
#  Le fichier publie est genere par publier-release.sh (adresse et empreinte).
# =============================================================================
set -euo pipefail
DEPOT="__DEPOT_URL__"
EMPREINTE="__EMPREINTE__"
CLE=/usr/share/keyrings/tlswitch-depot.gpg

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERREUR : %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "lance ce script en root (su - ou sudo -i)"
. /etc/os-release
[ "${ID:-}" = debian ] && [ "${VERSION_ID%%.*}" -ge 13 ] 2>/dev/null || die "Debian 13 requis (trouve : ${PRETTY_NAME:-inconnu})"
[ "$(dpkg --print-architecture)" = amd64 ] || die "processeur x86_64 (amd64) requis"
if [ -x /usr/local/sbin/tlswitch ]; then
  die "TL-SwitchOS est deja installe ($(/usr/local/sbin/tlswitch version 2>/dev/null)) : mises a jour par 'install update'"
fi

# --- questions du switch (Entree = valeur proposee) ---
demander() {  # <variable> <question> <defaut> <regex>
  local v="${!1:-}" r
  if [ -z "$v" ] && [ "${TLS_AUTO:-0}" != 1 ] && [ -t 0 ]; then
    while :; do
      read -rp "   $2 [$3] : " r; r=${r:-$3}
      printf '%s' "$r" | grep -qE "$4" && break
      echo "   valeur invalide"
    done
    v=$r
  fi
  printf -v "$1" '%s' "${v:-$3}"; export "${1?}"
}
say "TL-SwitchOS : installation sur cette Debian"
echo "   Les ports reseau deviennent des ports de switch au redemarrage."
echo "   L'adresse IP actuelle est gardee comme adresse de gestion (sinon DHCP)."
demander TLS_HOSTNAME "Nom du switch" tl-switch '^[A-Za-z0-9][A-Za-z0-9-]{0,62}$'
demander TLS_MGMT_VLAN "VLAN de gestion" 1 '^([1-9][0-9]{0,2}|[1-3][0-9]{3}|40[0-8][0-9]|409[0-4])$'

say "Depot TL-SwitchOS : $DEPOT"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
apt-get install -y -qq --no-install-recommends curl gpg ca-certificates >/dev/null
T=$(mktemp -d); chmod 755 "$T"; trap 'rm -rf "$T"' EXIT
curl -fsS --max-time 60 -o "$T/cle.gpg" "$DEPOT/tlswitch-depot.gpg" || die "cle du depot introuvable ($DEPOT)"
FP=$(gpg --show-keys --with-colons "$T/cle.gpg" 2>/dev/null | sed -n 's/^fpr:*\([0-9A-F]*\):*$/\1/p' | head -n 1)
[ -n "$FP" ] && [ "$FP" = "$EMPREINTE" ] || die "empreinte de la cle du depot inattendue : ${FP:-illisible}"
echo "   cle du depot verifiee : $FP"
install -d /usr/share/keyrings; install -m 644 "$T/cle.gpg" "$CLE"
cat > /etc/apt/sources.list.d/tlswitch.sources <<EOS
# Depot TL-SwitchOS : mises a jour du switch (show install updates / install update)
Types: deb
URIs: $DEPOT
Suites: stable
Components: main
Signed-By: $CLE
EOS
apt-get update -qq || die "depot injoignable (Internet ?)"

# --- paquet signe : telecharge par apt (signature du depot verifiee), puis deballe ---
say "Telechargement de TL-SwitchOS"
( cd "$T" && apt-get download -qq -o APT::Sandbox::User=root tlswitch ) || die "paquet tlswitch introuvable dans le depot"
dpkg-deb -x "$T"/tlswitch_*_all.deb "$T/deb"
TLSU=$(ls "$T"/deb/usr/share/tlswitch/TL-SwitchOS-*.tlsu | head -n 1)
tar xf "$TLSU" -C "$T" MANIFEST.json paquet.tar.gz
SUM=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['sha256'])" "$T/MANIFEST.json" 2>/dev/null \
      || sed -n 's/.*"sha256": *"\([0-9a-f]*\)".*/\1/p' "$T/MANIFEST.json")
echo "$SUM  $T/paquet.tar.gz" | sha256sum -c --quiet || die "paquet abime (empreinte differente)"
tar xzf "$T/paquet.tar.gz" -C "$T" --no-same-owner
# le switch garde le depot pour ses mises a jour
printf 'DEPOT_URL=%s\n' "$DEPOT" > "$T/tlswitch/depot-apt.conf"
cp "$CLE" "$T/tlswitch/depot-apt.gpg"
V=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$T/tlswitch/tlswitch.py" | head -n 1)
echo "   TL-SwitchOS $V"

# --- installation : le meme installateur que l'ISO ---
TLS_VERBOSE=${TLS_VERBOSE:-0} bash "$T/tlswitch/installer-tlswitch.sh"
# paquet Debian marque comme installe : apt suit les versions suivantes
apt-get install -y -qq "$T"/tlswitch_*_all.deb >/dev/null 2>&1 || true

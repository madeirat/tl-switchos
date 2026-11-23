#!/bin/bash
# =============================================================================
#  TL-SwitchOS - premier demarrage apres l'ISO
#  Affiche toute l'installation a l'ecran (ou sur la console serie).
#  Mode "assistant" : pose les questions (nom, IP, VLAN, mot de passe) avant.
# =============================================================================
DIR="${TLS_DIR:-/opt/tlswitch-install}"
LOG=/var/log/tlswitch-install.log
set -a
# shellcheck disable=SC1091
[ -f $DIR/iso.env ] && . $DIR/iso.env
set +a
MODE="${TLS_MODE:-auto}"

C='\033[1;36m'; G='\033[1;32m'; R='\033[1;31m'; Y='\033[1;33m'; N='\033[0m'
banner() {
  clear 2>/dev/null || printf '\033[2J\033[H'
  printf "${C}"
  cat <<'EOF'
  _____ _      ____          _ _       _      ___  ____
 |_   _| |    / ___|_      _(_) |_ ___| |__  / _ \/ ___|
   | | | |    \___ \ \ /\ / / | __/ __| '_ \| | | \___ \
   | | | |___  ___) \ V  V /| | || (__| | | | |_| |___) |
   |_| |_____||____/ \_/\_/ |_|\__\___|_| |_|\___/|____/
EOF
  printf "                 TECH-LINUX - installation${N}\n\n"
}

ask() {  # ask "question" "defaut" -> REPLY
  local q="$1" d="$2"
  if [ -n "$d" ]; then printf "  %s [${Y}%s${N}] : " "$q" "$d"; else printf "  %s : " "$q"; fi
  read -r REPLY || REPLY=""
  REPLY="${REPLY:-$d}"
}

valid_ip() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(/([0-9]{1,2}))?$ ]] || return 1
  local i
  for i in 1 2 3 4; do [ "${BASH_REMATCH[$i]}" -le 255 ] || return 1; done
  [ -z "${BASH_REMATCH[6]}" ] || { [ "${BASH_REMATCH[6]}" -ge 1 ] && [ "${BASH_REMATCH[6]}" -le 32 ]; }
}

list_ports() {
  local n=0
  for d in /sys/class/net/*; do
    [ -e "$d/device" ] && [ "$(cat "$d/type")" = 1 ] && [ ! -d "$d/wireless" ] || continue
    n=$((n+1))
    local b; b=$(basename "$d")
    local drv; drv=$(basename "$(readlink -f "$d/device/driver")" 2>/dev/null)
    local st="pas de lien"; [ "$(cat "$d/carrier" 2>/dev/null)" = 1 ] && st="lien OK"
    printf "    Gi1/0/%-3s %-10s %-10s %s  (%s)\n" "$n" "$b" "$drv" "$(cat "$d/address")" "$st"
  done
}

assistant() {
  banner
  echo "  Assistant de configuration. Entree = valeur proposee entre crochets."
  echo
  echo "  Ports detectes (ordre materiel) :"
  list_ports
  echo
  while :; do
    ask "Nom du switch" "${TLS_HOSTNAME:-tl-switch}"; TLS_HOSTNAME="$REPLY"
    [[ "$TLS_HOSTNAME" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,62}$ ]] && break
    printf "  ${R}lettres, chiffres et tirets uniquement${N}\n"
  done
  while :; do
    ask "VLAN de gestion (1 = tous les ports au depart)" "${TLS_MGMT_VLAN:-1}"; TLS_MGMT_VLAN="$REPLY"
    [[ "$TLS_MGMT_VLAN" =~ ^[0-9]+$ ]] && [ "$TLS_MGMT_VLAN" -ge 1 ] && [ "$TLS_MGMT_VLAN" -le 4094 ] && break
    printf "  ${R}nombre entre 1 et 4094${N}\n"
  done
  local d_ip="${TLS_IP:-dhcp}"
  while :; do
    ask "Adresse IP de gestion (ex : 192.168.1.30/24) ou dhcp" "$d_ip"; TLS_IP="$REPLY"
    [ "$TLS_IP" = dhcp ] && break
    if valid_ip "$TLS_IP"; then [[ "$TLS_IP" == */* ]] || TLS_IP="$TLS_IP/24"; break; fi
    printf "  ${R}adresse invalide${N}\n"
  done
  TLS_GW=""
  if [ "$TLS_IP" != dhcp ]; then
    local guess; guess=$(echo "${TLS_IP%/*}" | awk -F. '{print $1"."$2"."$3".1"}')
    while :; do
      ask "Passerelle" "$guess"; TLS_GW="$REPLY"
      valid_ip "$TLS_GW" && break
      printf "  ${R}adresse invalide${N}\n"
    done
    ask "Serveur DNS" "$TLS_GW"; TLS_DNS="$REPLY"
  fi
  echo
  echo "  Mot de passe du compte admin."
  echo "  Laisse vide pour admin / admin provisoire (a changer a la 1re connexion)."
  while :; do
    printf "  Mot de passe : "; read -rs P1; echo
    [ -z "$P1" ] && { TLS_ADMIN_PASS=""; break; }
    printf "  Confirmation : "; read -rs P2; echo
    if [ "$P1" != "$P2" ]; then printf "  ${R}differents${N}\n"; continue; fi
    if [ ${#P1} -lt 8 ]; then printf "  ${R}8 caracteres minimum${N}\n"; continue; fi
    TLS_ADMIN_PASS="$P1"; break
  done
  ask "Vitesse de la console serie" "${TLS_BAUD:-115200}"; TLS_BAUD="$REPLY"
  echo
  printf "  ${C}Recapitulatif${N}\n"
  echo "    Nom           : $TLS_HOSTNAME"
  echo "    Gestion       : Vlan$TLS_MGMT_VLAN, $TLS_IP${TLS_GW:+, passerelle $TLS_GW}"
  echo "    Compte admin  : $([ -n "$TLS_ADMIN_PASS" ] && echo 'mot de passe choisi' || echo 'admin / admin provisoire')"
  echo "    Console serie : ${TLS_SERIAL:-ttyS0} $TLS_BAUD bauds"
  echo
  ask "On installe ? (o/n)" "o"
  case "$REPLY" in o|O|oui|y|Y) ;; *) assistant; return ;; esac
  export TLS_HOSTNAME TLS_MGMT_VLAN TLS_IP TLS_GW TLS_ADMIN_PASS TLS_BAUD TLS_DNS
}

run_install() {
  banner
  printf "  Installation de TL-SwitchOS (journal complet : %s)\n" "$LOG"
  # tout est affiche ET garde dans le journal
  TLS_NONINTERACTIVE=1 TLS_VERBOSE=1 bash $DIR/installer-tlswitch.sh 2>&1 | tee -a "$LOG"
  return "${PIPESTATUS[0]}"
}

[ "$MODE" = assistant ] && assistant
until run_install; do
  printf "\n  ${R}L'installation a echoue.${N} Le detail est ci-dessus et dans %s\n" "$LOG"
  printf "  Entree = reessayer, s = shell de depannage, a = relancer l'assistant : "
  read -r a
  case "$a" in s) bash -l ;; a) assistant ;; esac
done
mkdir -p /etc/tlswitch && touch /etc/tlswitch/.installe
systemctl disable tlswitch-firstboot.service >/dev/null 2>&1
printf "\n  ${G}TL-SwitchOS est installe.${N} Redemarrage dans 10 secondes...\n"
sleep 10
systemctl reboot

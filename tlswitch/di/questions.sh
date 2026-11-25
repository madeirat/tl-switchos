#!/bin/sh
# TL-SwitchOS - questions posees DANS l'installateur Debian (comme OMV).
# Lance par preseed/early_command : juste apres langue et clavier, AVANT le reseau et le disque.
# Les reponses vont dans /tmp/tlswitch.env, lu par late.sh a la fin de l'installation.
. /usr/share/debconf/confmodule
# fichiers TL-SwitchOS : sur la cle (ISO) ou telecharges (iPXE)
S=/cdrom/tlswitch; [ -d $S ] || S=/tmp/tlswitch
debconf-loadtemplate tlswitch $S/di/templates
[ -f /tmp/tlswitch.env ] && exit 0   # deja repondu

err() { db_subst tlswitch/error MSG "$1"; db_fset tlswitch/error seen false; db_input critical tlswitch/error || true; db_go || true; }
ask() { db_fset "$1" seen false; db_input critical "$1" || true; db_go || true; db_get "$1"; }
pre() { db_set "$1" "$2"; db_fset "$1" seen true; }
is_ip() { echo "$1" | grep -Eq '^(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}$'; }

# valeurs proposees : celles passees a construire-iso.sh
if [ -f $S/iso.env ]; then
  . $S/iso.env
  [ -n "$TLS_HOSTNAME" ] && db_set tlswitch/hostname "$TLS_HOSTNAME"
  [ -n "$TLS_MGMT_VLAN" ] && db_set tlswitch/mgmt_vlan "$TLS_MGMT_VLAN"
  [ -n "$TLS_IP" ] && db_set tlswitch/ip "$TLS_IP"
  [ -n "$TLS_GW" ] && db_set tlswitch/gateway "$TLS_GW"
fi

# installation sans question : "tlsauto" sur la ligne du noyau (valeurs par defaut ou iso.env)
if grep -qw tlsauto /proc/cmdline; then
  db_get tlswitch/hostname; H="$RET"; db_get tlswitch/mgmt_vlan; V="$RET"
  pre netcfg/get_hostname "$H"; pre netcfg/hostname "$H"; pre netcfg/get_domain local
  printf "TLS_HOSTNAME='%s'\nTLS_MGMT_VLAN='%s'\n" "$H" "$V" > /tmp/tlswitch.env; : > /tmp/tlswitch.pass
  exit 0
fi
db_input critical tlswitch/intro || true
db_go || true

while :; do ask tlswitch/hostname; H="$RET"
  echo "$H" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9-]{0,62}$' && break
  err "Le nom ne peut contenir que des lettres, des chiffres et des tirets."; done
while :; do ask tlswitch/mgmt_vlan; V="$RET"
  echo "$V" | grep -Eq '^[0-9]{1,4}$' && [ "$V" -ge 1 ] && [ "$V" -le 4094 ] && break
  err "Le VLAN doit être un nombre entre 1 et 4094."; done
# l'adresse IP n'est PAS demandee ici : c'est l'etape reseau de Debian qui la configure
# (DHCP, ou saisie manuelle si pas de DHCP), et late.sh la reprend pour le switch.
# pas de mot de passe ici : le switch demarre avec admin / admin provisoire (affiche sur la
# console), et le nouveau mot de passe est demande a la premiere connexion
P1=""

# le nom donne remplace celui de l'installateur (et celui fourni par le DHCP)
db_set netcfg/get_hostname "$H"; db_fset netcfg/get_hostname seen true
db_set netcfg/hostname "$H"; db_fset netcfg/hostname seen true
db_set netcfg/get_domain local; db_fset netcfg/get_domain seen true

umask 077
{
  echo "TLS_HOSTNAME='$H'"
  echo "TLS_MGMT_VLAN='$V'"
} > /tmp/tlswitch.env
# mot de passe a part, jamais dans les journaux ; hache dans le systeme installe puis efface
printf '%s' "$P1" > /tmp/tlswitch.pass
exit 0

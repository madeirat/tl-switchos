#!/bin/sh
# TL-SwitchOS - choix du disque (lance par partman/early_command).
# UN seul disque interne : il est efface et partitionne sans question.
# Plusieurs disques : l'installateur demande lequel utiliser.
. /usr/share/debconf/confmodule
S=/cdrom/tlswitch; [ -d $S ] || S=/tmp/tlswitch

# questions du switch pas encore posees (ex. early_command indisponible) : on les pose ici
[ -f /tmp/tlswitch.env ] || sh $S/di/questions.sh

# heure : sans NTP (port 123 souvent filtre), une horloge fausse fait refuser les depots
# Debian par apt ("not valid yet" / "expired"). On prend l'heure dans la reponse HTTP
# du miroir, qui passe partout ou le miroir passe.
# jamais plus de 15 s : un miroir filtre ne doit pas bloquer l'installation
M=$(debconf-get mirror/http/hostname 2>/dev/null); M=${M:-deb.debian.org}
( wget -S --spider -T 10 "http://$M/" > /tmp/tls-date.txt 2>&1 ) & W=$!
n=0; while kill -0 $W 2>/dev/null && [ $n -lt 15 ]; do sleep 1; n=$((n+1)); done
kill $W 2>/dev/null
D=$(sed -n 's/^ *[Dd]ate: *//p' /tmp/tls-date.txt 2>/dev/null | head -1)
if [ -n "$D" ]; then
  date -u -D '%a, %d %b %Y %H:%M:%S' -s "${D% GMT}" >/dev/null 2>&1 && hwclock -w -u 2>/dev/null
  logger -t tlswitch "heure prise sur le miroir : $(date -u)"
else
  logger -t tlswitch "heure : miroir injoignable, horloge laissee telle quelle ($(date -u))"
fi

# disque qui porte le support d'installation (cle USB) : a exclure
# (pas d'awk dans l'installateur : uniquement sh, sed et grep de busybox)
SRC=$(sed -n 's#^/dev/\([^ ]*\) /cdrom .*#\1#p' /proc/mounts | sed -E 's#p?[0-9]+$##' | head -n 1)
DISQUES=""
for b in /sys/block/*; do
  n=${b##*/}
  case "$n" in loop*|ram*|sr*|fd*|zram*|md*|dm-*|mmcblk*boot*|nbd*) continue ;; esac
  [ -n "$SRC" ] && [ "$n" = "$SRC" ] && continue
  [ "$(cat "$b/removable" 2>/dev/null)" = 1 ] && continue         # cle USB, carte SD
  t=$(cat "$b/size" 2>/dev/null); [ "${t:-0}" -gt 4000000 ] || continue   # > 2 Go
  DISQUES="$DISQUES /dev/$n"
done
# shellcheck disable=SC2086
set -- $DISQUES
logger -t tlswitch "disques trouves :$DISQUES (support : ${SRC:-aucun})"
[ $# -eq 1 ] || exit 0   # 0 ou plusieurs : partman pose les questions normales

pre() { db_set "$1" "$2"; db_fset "$1" seen true; }
pre partman-auto/disk "$1"
pre partman-auto/method regular
pre partman-auto/choose_recipe atomic
pre partman-lvm/device_remove_lvm true
pre partman-md/device_remove_md true
pre partman-lvm/confirm true
pre partman-lvm/confirm_nooverwrite true
pre partman-partitioning/confirm_write_new_label true
pre partman/choose_partition finish
pre partman/confirm true
pre partman/confirm_nooverwrite true
pre grub-installer/bootdev "$1"
exit 0

#!/bin/sh
# TL-SwitchOS - choix du disque (lance par partman/early_command).
# Le disque est TOUJOURS choisi a la main : l'installateur demande lequel utiliser
# et comment le partitionner (guide ou manuel). Rien n'est efface sans confirmation.
# Seule exception : "tlsauto" sur la ligne du noyau (installation sans question) ET
# un seul disque interne : il est alors efface et partitionne automatiquement.
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
if ! grep -qw tlsauto /proc/cmdline; then
  # cas normal : partman pose les questions (disque, guide ou manuel).
  # Avant : liste des disques avec leur taille, et la cle d'installation signalee
  # (la choisir efface le support : formatage refuse puis installation cassee).
  LISTE=""
  for b in /sys/block/*; do
    n=${b##*/}
    case "$n" in loop*|ram*|sr*|fd*|zram*|md*|dm-*|mmcblk*boot*|nbd*) continue ;; esac
    t=$(cat "$b/size" 2>/dev/null); [ "${t:-0}" -gt 0 ] || continue; t=$(( t / 2097152 ))
    m=$(cat "$b/device/model" 2>/dev/null | sed 's/ *$//')
    e="$n $t Go${m:+ $m}"
    [ "$n" = "$SRC" ] && e="$e = CLE D'INSTALLATION, NE PAS CHOISIR"
    LISTE="${LISTE:+$LISTE ; }$e"
  done
  debconf-loadtemplate tlswitch $S/di/templates 2>/dev/null || true
  db_subst tlswitch/disques LISTE "${LISTE:-aucun}"
  if [ -n "$SRC" ]; then db_subst tlswitch/disques CLE "Ne choisis pas $SRC : c'est la cle d'installation. "
  else db_subst tlswitch/disques CLE ""; fi
  db_fset tlswitch/disques seen false; db_input critical tlswitch/disques || true; db_go || true
  # GRUB (BIOS) doit aller sur le disque choisi, pas sur le premier trouve :
  # apres l'installation de base, on lit le disque qui porte /target (ou /target/boot).
  mkdir -p /usr/lib/post-base-installer.d
  cat > /usr/lib/post-base-installer.d/50tlswitch-grub <<'G'
#!/bin/sh
. /usr/share/debconf/confmodule
P=$(sed -n 's#^\(/dev/[^ ]*\) /target/boot .*#\1#p' /proc/mounts | head -n 1)
[ -n "$P" ] || P=$(sed -n 's#^\(/dev/[^ ]*\) /target .*#\1#p' /proc/mounts | head -n 1)
case "$P" in /dev/mapper/*|/dev/md*|/dev/dm-*|"") exit 0 ;; esac   # LVM/RAID : choix par defaut
D=$(echo "$P" | sed -E 's#p?[0-9]+$##')
[ -b "$D" ] || exit 0
db_set grub-installer/bootdev "$D"; db_fset grub-installer/bootdev seen true
logger -t tlswitch "GRUB sur $D (disque de $P)"
exit 0
G
  chmod +x /usr/lib/post-base-installer.d/50tlswitch-grub
  exit 0
fi
[ $# -eq 1 ] || exit 0   # 0 ou plusieurs disques : partman pose les questions normales

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

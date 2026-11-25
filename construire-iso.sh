#!/bin/bash
# =============================================================================
#  TL-SwitchOS - fabrique l'ISO d'installation automatique
#
#  A lancer en root sur une machine Debian/Ubuntu/Proxmox reliee a Internet,
#  depuis le dossier qui contient ce script et le dossier "tlswitch/".
#
#     bash construire-iso.sh
#
#  Variables optionnelles (sinon valeurs par defaut ou questions) :
#     TLS_ADMIN_PASS=...        mot de passe admin definitif (sinon admin / admin, a changer
#                               obligatoirement a la premiere connexion)
#     TLS_HOSTNAME=SWITCH01     nom du switch            (defaut : tl-switch)
#     TLS_IP=192.168.1.30/24    IP de gestion fixe       (defaut : dhcp)
#     TLS_GW=192.168.1.1        passerelle
#     TLS_MGMT_VLAN=99          VLAN de gestion          (defaut : 1)
#     TLS_SERIAL=ttyS0 TLS_BAUD=115200
#     DEBIAN_ISO=/chemin/debian-13.x.x-amd64-netinst.iso   (sinon telechargee)
#
#  Resultat : TL-SwitchOS-<version>-amd64.iso, a graver sur une cle USB
#  (Rufus en mode DD, Etcher, Ventoy, ou dd). BIOS et UEFI.
#
#  Installation par le reseau (iPXE) :
#     bash construire-iso.sh --ipxe http://192.168.1.5/tlswitch
#  produit le dossier TL-SwitchOS-<version>-ipxe/ a copier TEL QUEL sur ce
#  serveur HTTP (a cette adresse), avec le menu tlswitch.ipxe a appeler depuis
#  ton iPXE :  chain http://192.168.1.5/tlswitch/tlswitch.ipxe
#
#  Deroulement (facon OpenMediaVault) : menu GRUB TL-SwitchOS, puis l'installateur
#  Debian aux couleurs TL-SwitchOS pose aussi les questions du switch (nom, VLAN,
#  IP, mot de passe), demande le disque, et installe TOUT d'une traite. Au
#  redemarrage le switch est pret.
# =============================================================================
set -euo pipefail
IPXE=""
if [ "${1:-}" = "--ipxe" ]; then
  IPXE="${2:?adresse HTTP du dossier, ex : http://192.168.1.5/tlswitch}"; IPXE="${IPXE%/}"
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$HERE/tlswitch"
[ -f "$PKG/tlswitch.py" ] || { echo "dossier tlswitch/ introuvable a cote du script"; exit 1; }
VERSION=$(python3 "$PKG/tlswitch.py" version 2>/dev/null || echo 1.0.0)
OUT="$HERE/TL-SwitchOS-$VERSION-amd64.iso"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

HOST="${TLS_HOSTNAME:-tl-switch}"
IP="${TLS_IP:-dhcp}"
GW="${TLS_GW:-}"
VLAN="${TLS_MGMT_VLAN:-1}"
SERIAL="${TLS_SERIAL:-ttyS0}"
BAUD="${TLS_BAUD:-115200}"
UNIT="${SERIAL#ttyS}"

echo "==> Outils"
command -v xorriso >/dev/null || { apt-get update -qq && apt-get install -y -qq xorriso; }
command -v wget >/dev/null || apt-get install -y -qq wget
command -v cpio >/dev/null || apt-get install -y -qq cpio

echo "==> Image Debian netinst"
if [ -n "$IPXE" ]; then
  echo "   (mode iPXE : noyau et initrd netboot telecharges plus loin)"
elif [ -z "${DEBIAN_ISO:-}" ]; then
  # 1) une netinst deja presente ? (dossier d'appel, projet, dossier parent, Telechargements)
  UH=$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6); UH=${UH:-$HOME}
  for d in "${TLS_APPELANT:-$PWD}" "$HERE" "$(dirname "$HERE")" "$UH" "$UH/Downloads" "$UH/Téléchargements" "$UH/Telechargements"; do
    for f in "$d"/debian-*-amd64-netinst.iso; do
      [ -f "$f" ] || continue
      if [ -z "${DEBIAN_ISO:-}" ] || [ "$f" -nt "$DEBIAN_ISO" ]; then DEBIAN_ISO="$f"; fi
    done
  done
  if [ -n "${DEBIAN_ISO:-}" ]; then
    echo "   netinst deja presente, pas de telechargement : $DEBIAN_ISO"
  else
    # 2) sinon on la telecharge (une seule fois : elle est gardee a cote du projet)
    BASE=https://cdimage.debian.org/debian-cd/current/amd64/iso-cd
    wget -q -O "$WORK/SHA256SUMS" "$BASE/SHA256SUMS"
    NAME=$(awk '/netinst.iso$/ && !/edu|mac/ {print $2; exit}' "$WORK/SHA256SUMS")
    [ -n "$NAME" ] || { echo "netinst introuvable sur $BASE"; exit 1; }
    DEBIAN_ISO="$(dirname "$HERE")/$NAME"
    echo "   telechargement de $NAME (une seule fois)"
    wget -q --show-progress -O "$DEBIAN_ISO.part" "$BASE/$NAME"
    (cd "$WORK" && sed -n "s| $NAME\$| $DEBIAN_ISO.part|p" SHA256SUMS | sha256sum -c - >/dev/null) || \
      { echo "somme SHA256 incorrecte"; rm -f "$DEBIAN_ISO.part"; exit 1; }
    mv "$DEBIAN_ISO.part" "$DEBIAN_ISO"
  fi
fi
[ -n "$IPXE" ] || [ -f "$DEBIAN_ISO" ] || { echo "ISO Debian introuvable : $DEBIAN_ISO"; exit 1; }
[ -n "$IPXE" ] || echo "   base : $(basename "$DEBIAN_ISO")"

# root est verrouille sur le switch : on lui donne un mot de passe aleatoire jetable pour l'installateur
ROOTHASH=$(head -c 32 /dev/urandom | base64 | openssl passwd -6 -stdin)
ADMINHASH=""
if [ -n "${TLS_ADMIN_PASS:-}" ]; then ADMINHASH=$(printf '%s\n' "$TLS_ADMIN_PASS" | openssl passwd -6 -stdin); fi

echo "==> Cle de signature des mises a jour"
install -d -m 700 "$HERE/cles"
if [ ! -f "$HERE/cles/maj-privee.pem" ]; then
  openssl genpkey -algorithm ed25519 -out "$HERE/cles/maj-privee.pem"; chmod 600 "$HERE/cles/maj-privee.pem"
  echo "   nouvelle cle : $HERE/cles/maj-privee.pem (A SAUVEGARDER, jamais sur GitHub)"
fi
openssl pkey -in "$HERE/cles/maj-privee.pem" -pubout -out "$HERE/cles/maj-publique.pem"
cp "$HERE/cles/maj-publique.pem" "$PKG/maj-publique.pem"

echo "==> Preparation"
mkdir -p "$WORK/iso/tlswitch"
cp -r "$PKG/." "$WORK/iso/tlswitch/"
rm -rf "$WORK/iso/tlswitch/__pycache__"
# reglages transmis a l'installateur (lu par la late_command)
cat > "$WORK/iso/tlswitch/iso.env" <<EOF
TLS_NONINTERACTIVE=1
TLS_HOSTNAME='$HOST'
TLS_IP='$IP'
TLS_GW='$GW'
TLS_MGMT_VLAN='$VLAN'
TLS_SERIAL='$SERIAL'
TLS_BAUD='$BAUD'
TLS_ADMIN_HASH='$ADMINHASH'
TLS_LOCK_ROOT=1
TLS_MODE='${TLS_MODE:-assistant}'
EOF

# PAS de auto=true : il repousse le choix du clavier APRES le reseau (donc apres nos questions)
KARGS="priority=critical preseed/file=/cdrom/preseed.cfg locale=fr_FR.UTF-8 language=fr country=FR keymap=fr keyboard-configuration/xkb-keymap=fr hostname=$HOST domain=local"
# plus de "quiet" : tout s'affiche

cat > "$WORK/iso/preseed.cfg" <<EOF
# TL-SwitchOS - installation de Debian (le disque est demande), puis assistant TL-SwitchOS au 1er demarrage
d-i debian-installer/locale string fr_FR.UTF-8
d-i keyboard-configuration/xkb-keymap select fr
d-i keyboard-configuration/layoutcode string fr
d-i keyboard-configuration/variant select Français
d-i console-setup/ask_detect boolean false
d-i debian-installer/language string fr
d-i debian-installer/country string FR
# questions TL-SwitchOS EN PREMIER (juste apres langue/clavier, avant le reseau)
d-i preseed/early_command string sh /cdrom/tlswitch/di/questions.sh
d-i netcfg/choose_interface select auto
d-i netcfg/link_wait_timeout string 20
d-i netcfg/dhcp_timeout string 60
d-i netcfg/get_hostname string $HOST
d-i netcfg/get_domain string local
d-i netcfg/hostname string $HOST
d-i hw-detect/load_firmware boolean true
d-i mirror/country string manual
d-i mirror/http/hostname string deb.debian.org
d-i mirror/http/directory string /debian
d-i mirror/http/proxy string
d-i passwd/root-login boolean true
d-i passwd/make-user boolean false
d-i passwd/root-password-crypted password $ROOTHASH
d-i clock-setup/utc boolean true
d-i time/zone string Europe/Paris
# pas de synchro NTP dans l'installateur (bloque si NTP est filtre) : le switch se
# synchronise ensuite tout seul avec systemd-timesyncd (ntp server en CLI)
d-i clock-setup/ntp boolean false
# disque : toujours choisi a la main pendant l'installation (sauf tlsauto avec un seul disque)
d-i partman/early_command string sh /cdrom/tlswitch/di/disque.sh
d-i partman-efi/non_efi_system boolean true
d-i apt-setup/cdrom/set-first boolean false
d-i apt-setup/non-free-firmware boolean true
d-i apt-setup/contrib boolean false
# systeme minimal (pas de "standard"), sans paquets recommandes. Les paquets du switch
# ne sont PAS dans pkgsel : un seul paquet introuvable y fait echouer toute l'etape
# "Choisir et installer des logiciels". Ils sont installes ensuite par TL-SwitchOS,
# paquet par paquet, avec reprises (un paquet optionnel absent ne bloque rien).
tasksel tasksel/first multiselect ssh-server
d-i base-installer/install-recommends boolean false
d-i apt-setup/services-select multiselect security, updates
iperf3 iperf3/start_daemon boolean false
d-i pkgsel/include string openssh-server python3
# pas de mise a jour complete ici (plus rapide, et pas d'echec si un miroir est lent)
d-i pkgsel/upgrade select none
popularity-contest popularity-contest/participate boolean false
d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean false
d-i grub-installer/bootdev string default
# installation de TL-SwitchOS dans le systeme installe
d-i preseed/late_command string sh /cdrom/tlswitch/di/late.sh
d-i finish-install/reboot_in_progress note
d-i cdrom-detect/eject boolean true
EOF

# =============================================================================
#  Mode iPXE : dossier a poser sur un serveur HTTP
# =============================================================================
if [ -n "$IPXE" ]; then
  SUITE="${DEBIAN_SUITE:-trixie}"
  NB="${DEBIAN_NETBOOT_URL:-https://deb.debian.org/debian/dists/$SUITE/main/installer-amd64/current/images}"
  DIR="$HERE/TL-SwitchOS-$VERSION-ipxe"
  echo "==> Installateur Debian netboot ($SUITE)"
  rm -rf "$DIR"; mkdir -p "$DIR"
  wget -q -O "$WORK/SUMS" "$NB/SHA256SUMS"
  for f in netboot/debian-installer/amd64/linux:linux netboot/debian-installer/amd64/initrd.gz:initrd.gz \
           netboot/gtk/debian-installer/amd64/initrd.gz:gtk-initrd.gz; do
    src="${f%%:*}"; dst="${f##*:}"
    wget -q -O "$DIR/$dst" "$NB/$src"
    want=$(awk -v p="./$src" '$2==p {print $1}' "$WORK/SUMS")
    [ -n "$want" ] && [ "$(sha256sum "$DIR/$dst" | cut -d' ' -f1)" = "$want" ] || { echo "somme SHA256 incorrecte : $src"; exit 1; }
    echo "   $dst : OK"
  done
  # les fichiers TL-SwitchOS sont telecharges par l'installateur au debut (pas de /cdrom en reseau)
  tar czf "$DIR/tlswitch.tar.gz" --owner=0 --group=0 -C "$WORK/iso" tlswitch
  sed -e 's#/cdrom/tlswitch#/tmp/tlswitch#g' -e '/cdrom-detect/d' -e '/apt-setup\/cdrom/d' -e '/preseed\/early_command/d' "$WORK/iso/preseed.cfg" > "$DIR/preseed.cfg"
  echo "d-i preseed/early_command string wget -q -O /tmp/tls.tgz $IPXE/tlswitch.tar.gz && cd /tmp && zcat tls.tgz | tar x && sh /tmp/tlswitch/di/questions.sh" >> "$DIR/preseed.cfg"
  B="$PKG/boot"; mkdir -p "$WORK/logo/usr/share/graphics"
  cp "$B"/logo_installer.png "$B"/logo_installer_dark.png "$WORK/logo/usr/share/graphics/"
  (cd "$WORK/logo" && find . | cpio -o -H newc -R 0:0 --quiet | gzip -9) > "$DIR/tlswitch-logo.gz"
  NARGS="auto=true priority=critical url=$IPXE/preseed.cfg locale=fr_FR.UTF-8 language=fr country=FR keymap=fr hostname=$HOST domain=local netcfg/choose_interface=auto"
  cat > "$DIR/tlswitch.ipxe" <<EOF
#!ipxe
# TL-SwitchOS $VERSION - installation par le reseau
set tls $IPXE
:menu
menu TL-SwitchOS $VERSION - TECH-LINUX
item --gap -- Installation (le disque est demande, rien n'est efface sans confirmation)
item texte    Installer TL-SwitchOS $VERSION
item graph    Installer TL-SwitchOS $VERSION (mode graphique)
item serie    Installer TL-SwitchOS $VERSION via la console serie ($BAUD bauds)
item --gap --
item retour   Retour
choose --default texte --timeout 15000 cible || goto retour
goto \${cible}
:texte
kernel \${tls}/linux initrd=initrd.gz $NARGS tlstty=ecran console=tty0 ---
initrd \${tls}/initrd.gz
boot || goto menu
:graph
kernel \${tls}/linux initrd=gtk-initrd.gz initrd=tlswitch-logo.gz $NARGS tlstty=ecran vga=788 console=tty0 ---
initrd \${tls}/gtk-initrd.gz
initrd \${tls}/tlswitch-logo.gz
boot || goto menu
:serie
kernel \${tls}/linux initrd=initrd.gz $NARGS tlstty=serie console=tty0 console=$SERIAL,${BAUD}n8 --- console=$SERIAL,${BAUD}n8
initrd \${tls}/initrd.gz
boot || goto menu
:retour
exit
EOF
  cat > "$DIR/LISEZMOI.txt" <<EOF
TL-SwitchOS $VERSION - installation par le reseau (iPXE)

1. Copie tout ce dossier sur ton serveur HTTP pour qu'il reponde a :
     $IPXE/tlswitch.ipxe
   (l'adresse est inscrite dans les fichiers : si elle change, relance
    bash construire-iso.sh --ipxe <nouvelle adresse>)
2. Dans ton menu iPXE, ajoute par exemple :
     item tlswitch TL-SwitchOS $VERSION
     ...
     :tlswitch
     chain $IPXE/tlswitch.ipxe
3. Le switch doit avoir Internet pendant l'installation (paquets Debian).
   HTTP simple conseille (pas HTTPS) : c'est ce que iPXE et l'installateur gerent le mieux.
EOF
  sha256sum "$DIR"/* > "$WORK/s" && mv "$WORK/s" "$DIR/SHA256SUMS"
  echo
  echo "Dossier iPXE pret : $DIR"
  cat "$DIR/LISEZMOI.txt"
  exit 0
fi

# --- habillage : theme GRUB, fond isolinux, logo de l'installateur ---
B="$PKG/boot"
mkdir -p "$WORK/iso/boot/grub/themes/tlswitch" "$WORK/logo/usr/share/graphics"
cp "$B"/theme.txt "$B"/grub-fond.png "$B"/select_*.png "$B"/*.pf2 "$WORK/iso/boot/grub/themes/tlswitch/"
cp "$B"/logo_installer.png "$B"/logo_installer_dark.png "$WORK/logo/usr/share/graphics/"
# petite archive ajoutee a l'initrd graphique : remplace le bandeau Debian par celui de TL-SwitchOS
(cd "$WORK/logo" && find . | cpio -o -H newc -R 0:0 --quiet | gzip -9) > "$WORK/iso/tlswitch-logo.gz"
GTK=/install.amd/gtk/initrd.gz
xorriso -indev "$DEBIAN_ISO" -find $GTK 2>/dev/null | grep -q initrd.gz || GTK=/install.amd/initrd.gz
# a l'ecran : uniquement tty0 (une console serie en plus peut faire partir l'installateur
# sur un port serie absent : l'ecran reste fige apres les messages USB)
TEXTE="$KARGS tlstty=ecran console=tty0 ---"
ECRAN="$KARGS tlstty=ecran vga=788 console=tty0 ---"
SERIE="$KARGS tlstty=serie console=tty0 console=$SERIAL,${BAUD}n8 --- console=$SERIAL,${BAUD}n8"

# --- menu UEFI (GRUB) : theme TL-SwitchOS a l'ecran, menu texte sur la console serie ---
cat > "$WORK/iso/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=15
set TH=/boot/grub/themes/tlswitch
insmod all_video
insmod gfxterm
insmod png
insmod serial
serial --unit=$UNIT --speed=$BAUD
terminal_input console serial
if loadfont /boot/grub/font.pf2 ; then
  set gfxmode=1024x768,auto
  terminal_output gfxterm serial
  loadfont \$TH/police-mono-16.pf2
  loadfont \$TH/police-16.pf2
  loadfont \$TH/police-19.pf2
  loadfont \$TH/police-gras-19.pf2
  set theme=\$TH/theme.txt
else
  terminal_output console serial
fi
menuentry "Installer TL-SwitchOS $VERSION" {
    linux /install.amd/vmlinuz $TEXTE
    initrd /install.amd/initrd.gz
}
menuentry "Installer TL-SwitchOS $VERSION (mode graphique)" {
    linux /install.amd/vmlinuz $ECRAN
    initrd $GTK /tlswitch-logo.gz
}
menuentry "Installer via la console série ($BAUD bauds)" {
    linux /install.amd/vmlinuz $SERIE
    initrd /install.amd/initrd.gz
}
menuentry "Installation Debian manuelle" {
    linux /install.amd/vmlinuz vga=788 ---
    initrd /install.amd/initrd.gz
}
EOF

# --- menu BIOS (isolinux) : meme habillage ---
mkdir -p "$WORK/iso/isolinux"
cp "$B/isolinux-fond.png" "$WORK/iso/isolinux/isolinux-fond.png"
cat > "$WORK/iso/isolinux/isolinux.cfg" <<EOF
serial $UNIT $BAUD
ui vesamenu.c32
menu resolution 640 480
menu background isolinux-fond.png
menu title
menu vshift 13
menu rows 5
menu margin 19
menu width 80
menu tabmsgrow 25
menu cmdlinerow 25
menu timeoutrow 26
menu tabmsg Entree : demarrer    Tab : modifier
menu autoboot Demarrage automatique dans # s
menu color screen    37;40 #00000000 #00000000 none
menu color border    30;44 #00000000 #00000000 none
menu color title     1;36;44 #00000000 #00000000 none
menu color unsel     37;44 #ffc9d4e8 #00000000 none
menu color hotkey    1;37;44 #ffffffff #00000000 none
menu color sel       7;37;40 #ffffffff #e03b82f6 none
menu color hotsel    1;7;37;40 #ffffffff #e03b82f6 none
menu color tabmsg    31;40 #ff8fa0bd #00000000 none
menu color timeout_msg 37;40 #ff22d3ee #00000000 none
menu color timeout   1;37;40 #ff22d3ee #00000000 none
menu color cmdline   37;40 #ffffffff #00000000 none
timeout 150
default tlswitch
label tlswitch
  menu label ^Installer TL-SwitchOS $VERSION
  kernel /install.amd/vmlinuz
  append initrd=/install.amd/initrd.gz $TEXTE
label graphique
  menu label Installer en mode ^graphique
  kernel /install.amd/vmlinuz
  append initrd=$GTK,/tlswitch-logo.gz $ECRAN
label serie
  menu label Installer via ^console serie
  kernel /install.amd/vmlinuz
  append initrd=/install.amd/initrd.gz $SERIE
label manuel
  menu label Installation Debian ^manuelle
  kernel /install.amd/vmlinuz
  append initrd=/install.amd/initrd.gz vga=788 ---
EOF

echo "==> Construction de l'ISO"
MAPS=(-map "$WORK/iso/preseed.cfg" /preseed.cfg
      -map "$WORK/iso/tlswitch" /tlswitch
      -map "$WORK/iso/boot/grub/grub.cfg" /boot/grub/grub.cfg
      -map "$WORK/iso/boot/grub/themes/tlswitch" /boot/grub/themes/tlswitch
      -map "$WORK/iso/tlswitch-logo.gz" /tlswitch-logo.gz)
if xorriso -indev "$DEBIAN_ISO" -find /isolinux/isolinux.cfg 2>/dev/null | grep -q isolinux.cfg; then
  MAPS+=(-map "$WORK/iso/isolinux/isolinux.cfg" /isolinux/isolinux.cfg
         -map "$WORK/iso/isolinux/isolinux-fond.png" /isolinux/isolinux-fond.png)
fi
rm -f "$OUT"
xorriso -indev "$DEBIAN_ISO" -outdev "$OUT" \
  "${MAPS[@]}" -chown_r 0 / -- -chgrp_r 0 / -- -boot_image any replay 2>&1 | grep -Ei 'error|fail|written' || true
[ -s "$OUT" ] || { echo "echec de construction"; exit 1; }
sha256sum "$OUT" | tee "$OUT.sha256"
cat <<EOF

ISO prete : $OUT

 1. Grave-la sur une cle USB (Rufus en mode "DD", balenaEtcher, ou :
      dd if=$(basename "$OUT") of=/dev/sdX bs=4M status=progress)
 2. Branche UN port du switch sur un reseau qui a acces a Internet
    (l'installation telecharge les paquets Debian).
 3. Demarre sur la cle : l'installateur pose les questions du switch (nom et
    VLAN de gestion), puis la config reseau Debian (DHCP ou IP fixe), le disque a utiliser et son partitionnement, puis installe tout.
 4. Connexion : console serie $SERIAL $BAUD 8N1, SSH ou https://
    Compte admin / admin : le nouveau mot de passe est demande a la premiere connexion.
EOF

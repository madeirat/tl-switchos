#!/bin/bash
# =============================================================================
#  TL-SwitchOS - fabrique l'IMAGE COMPACTE pour les petites machines
#  (mini PC, clients legers, boitiers type Streamcore, cartes ARM multiport)
#
#  A lancer en root sur une Debian 13 reliee a Internet (le LXC de construction
#  convient), depuis le dossier qui contient ce script et "tlswitch/" :
#
#     bash construire-image.sh                 PC x86_64 (BIOS et UEFI)
#     bash construire-image.sh arm64           cartes ARM 64 bits demarrant en UEFI
#                                              (U-Boot recent, EDK2...)
#     bash construire-image.sh arm64 --carte nanopi-r4s-rk3399
#                                              + U-Boot Debian de la carte ecrit au
#                                                debut de l'image (carte SD)
#
#  Variables optionnelles :
#     SUITE=trixie  MIROIR=http://deb.debian.org/debian   SECURITE=http://security.debian.org/debian-security
#     NOYAU=linux-image-amd64         paquet du noyau (defaut : linux-image-<arch>)
#     SLOT_MO=300  BOOT_MO=64  DATA_MO=64   tailles des partitions (Mo)
#     TLS_HOSTNAME, TLS_MGMT_VLAN, TLS_IP, TLS_GW, TLS_DNS : reglages par defaut
#                                     ecrits dans tlswitch.env (modifiable apres coup)
#
#  Resultat :
#     TL-SwitchOS-<version>-<arch>.img.xz   image disque complete (~ 700 Mo une fois
#                                           decompressee), a ecrire sur la cle, la CF,
#                                           le SATA DOM ou la carte SD
#     TL-SwitchOS-<version>-<arch>.tlsi     mise a jour du systeme (switchs deja en
#                                           image compacte : install image ...)
#     TL-SwitchOS-<version>-<arch>-live.iso live CD (CD ou cle USB, BIOS et UEFI) : essayer
#                                           TL-SwitchOS sans rien ecrire (config en memoire),
#                                           puis l'installer sur le disque interne
#
#  Organisation du disque (tient dans 1 Go) :
#     16 Mo libres (U-Boot des cartes ARM) | TLSBOOT 64 Mo FAT (GRUB, reglages)
#     | systeme A 300 Mo | systeme B 300 Mo | TLSDATA (le reste, agrandi au 1er demarrage)
#  Le systeme est en lecture seule (squashfs). Une mise a jour s'ecrit dans l'autre
#  slot et n'est gardee que si elle demarre : sinon le switch revient tout seul.
# =============================================================================
set -euo pipefail
ARCH=amd64; CARTE=""
while [ $# -gt 0 ]; do
  case "$1" in
    amd64|arm64) ARCH=$1 ;;
    --carte) CARTE="${2:?nom de la carte, ex : nanopi-r4s-rk3399}"; shift ;;
    *) echo "usage : bash construire-image.sh [amd64|arm64] [--carte <carte>]"; exit 1 ;;
  esac
  shift
done
HERE="$(cd "$(dirname "$0")" && pwd)"
PKG="$HERE/tlswitch"
[ -f "$PKG/tlswitch.py" ] || { echo "dossier tlswitch/ introuvable a cote du script"; exit 1; }
[ "$(id -u)" = 0 ] || { echo "a lancer en root"; exit 1; }
VERSION=$(python3 "$PKG/tlswitch.py" version)
SUITE="${SUITE:-trixie}"
MIROIR="${MIROIR:-http://deb.debian.org/debian}"
SECURITE="${SECURITE-http://security.debian.org/debian-security}"
COMPOSANTS="${COMPOSANTS:-main non-free-firmware}"
NOYAU="${NOYAU:-linux-image-$ARCH}"
SLOT_MO="${SLOT_MO:-300}"; BOOT_MO="${BOOT_MO:-64}"; DATA_MO="${DATA_MO:-64}"
NOM="TL-SwitchOS-$VERSION-$ARCH"
IMAGE_ID="$VERSION-$ARCH-$(date +%Y%m%d%H%M)"
OUT="$HERE/$NOM.img"
W=$(mktemp -d /var/tmp/tlsimage.XXXX)
R="$W/racine"
nettoyer() {
  set +e
  for m in media/boot dev/pts dev proc sys run; do mountpoint -q "$R/$m" && umount -l "$R/$m"; done
  rm -rf "$W"
}
trap nettoyer EXIT
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
dans() { chroot "$R" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8 \
           DEBIAN_FRONTEND=noninteractive "$@"; }

case $ARCH in
  amd64) GRUB_BIN="grub-pc-bin grub-efi-amd64-bin"; BCJ=x86; KCMD="console=ttyS0,115200n8 console=tty0" ;;
  arm64) GRUB_BIN="grub-efi-arm64-bin"; BCJ=arm; KCMD="" ;;   # console : celle du device tree
esac

say "Outils"
P="debootstrap squashfs-tools dosfstools mtools fdisk e2fsprogs xz-utils openssl python3 xorriso"
[ "$(dpkg --print-architecture)" != "$ARCH" ] && P="$P qemu-user-static binfmt-support"
# shellcheck disable=SC2086
for p in $P; do dpkg -s "$p" >/dev/null 2>&1 || NEED="${NEED:-} $p"; done
# shellcheck disable=SC2086
[ -z "${NEED:-}" ] || { apt-get update -qq && apt-get install -y -qq $NEED >/dev/null; }

say "Cle de signature des mises a jour"
install -d -m 700 "$HERE/cles"
if [ ! -f "$HERE/cles/maj-privee.pem" ]; then
  openssl genpkey -algorithm ed25519 -out "$HERE/cles/maj-privee.pem"; chmod 600 "$HERE/cles/maj-privee.pem"
  echo "   nouvelle cle : $HERE/cles/maj-privee.pem (A SAUVEGARDER, jamais sur GitHub)"
fi
openssl pkey -in "$HERE/cles/maj-privee.pem" -pubout -out "$HERE/cles/maj-publique.pem"
cp "$HERE/cles/maj-publique.pem" "$PKG/maj-publique.pem"

say "Systeme de base ($SUITE, $ARCH)"
debootstrap --variant=minbase --arch="$ARCH" --components="${COMPOSANTS// /,}" \
  --include=ca-certificates "$SUITE" "$R" "$MIROIR" > "$W/debootstrap.log" 2>&1 \
  || { tail -20 "$W/debootstrap.log"; exit 1; }
for m in proc sys dev dev/pts run; do mount --bind "/$m" "$R/$m"; done
printf '#!/bin/sh\nexit 101\n' > "$R/usr/sbin/policy-rc.d"; chmod 755 "$R/usr/sbin/policy-rc.d"
cp -L /etc/resolv.conf "$R/etc/resolv.conf" 2>/dev/null || true
{
  echo "deb $MIROIR $SUITE $COMPOSANTS"
  echo "deb $MIROIR $SUITE-updates $COMPOSANTS"
  [ -n "$SECURITE" ] && echo "deb $SECURITE $SUITE-security $COMPOSANTS"
} > "$R/etc/apt/sources.list"
# rien d'inutile sur une petite flash : ni documentation, ni pages de manuel, ni traductions
cat > "$R/etc/dpkg/dpkg.cfg.d/01-tlswitch-compact" <<'EOF'
path-exclude /usr/share/doc/*
path-include /usr/share/doc/*/copyright
path-exclude /usr/share/man/*
path-exclude /usr/share/info/*
path-exclude /usr/share/lintian/*
path-exclude /usr/share/locale/*
path-include /usr/share/locale/fr/*
path-include /usr/share/locale/locale.alias
EOF
cat > "$R/etc/apt/apt.conf.d/90-tlswitch-compact" <<'EOF'
APT::Install-Recommends "false";
APT::Install-Suggests "false";
Acquire::Languages "none";
EOF
cat > "$R/etc/tlswitch-image" <<EOF
IMAGE=$IMAGE_ID
VERSION=$VERSION
ARCH=$ARCH
SUITE=$SUITE
EOF
echo tl-switch > "$R/etc/hostname"
printf "127.0.0.1\tlocalhost\n127.0.1.1\ttl-switch\n::1\t\tlocalhost ip6-localhost ip6-loopback\n" > "$R/etc/hosts"
install -d "$R/etc/initramfs-tools/conf.d"
printf 'MODULES=most\nCOMPRESS=xz\nRESUME=none\n' > "$R/etc/initramfs-tools/conf.d/tlswitch"

say "Noyau et demarrage"
dans apt-get update -qq
# shellcheck disable=SC2086
dans apt-get install -y -qq systemd-sysv udev kmod initramfs-tools xz-utils fdisk e2fsprogs dosfstools \
  grub-common $GRUB_BIN "$NOYAU" python3 openssh-server iproute2 >/dev/null
say "Allegement du noyau (pilotes inutiles a un switch)"
for K in "$R"/lib/modules/*; do
  for d in sound drivers/gpu drivers/media drivers/net/wireless drivers/bluetooth drivers/infiniband \
           drivers/staging drivers/iio drivers/isdn drivers/input/touchscreen drivers/video/fbdev/via \
           drivers/net/can drivers/net/ethernet/mellanox/mlx5 drivers/scsi/lpfc drivers/scsi/qla2xxx \
           drivers/scsi/bfa drivers/scsi/qedf drivers/scsi/be2iscsi drivers/net/ethernet/qlogic/qed \
           net/wireless net/mac80211 net/bluetooth fs/btrfs fs/xfs fs/ocfs2 fs/gfs2 fs/nfsd fs/ceph \
           fs/smb fs/f2fs fs/jfs fs/nilfs2 fs/ubifs; do
    rm -rf "${K:?}/kernel/$d"
  done
  dans depmod "$(basename "$K")"
done
du -sh "$R"/lib/modules/* | sed 's/^/   modules : /'

say "TL-SwitchOS $VERSION (sans les ports : detectes au premier demarrage de la machine)"
rm -rf "$R/opt/tlswitch-install"; install -d "$R/opt/tlswitch-install"
cp -r "$PKG/." "$R/opt/tlswitch-install/"; rm -rf "$R/opt/tlswitch-install/__pycache__"
dans /usr/bin/env TLS_ETAPES="1 2 6 7 8 9" TLS_NONINTERACTIVE=1 TLS_VERBOSE=0 \
  bash /opt/tlswitch-install/installer-tlswitch.sh 2>&1 | sed 's/^/   /'
[ -x "$R/usr/local/sbin/tlswitch-image" ] || { echo "integration de l'image compacte absente (maj.sh)"; exit 1; }
rm -rf "$R/opt/tlswitch-install"
# firmwares des cartes reseau : installes par installer-tlswitch.sh (liste FW, la meme que
# l'ISO standard). On retire ceux des cartes graphiques, du Wi-Fi et du Bluetooth qu'ils amenent.
FWD="$R/usr/lib/firmware"; [ -d "$FWD" ] || FWD="$R/lib/firmware"
if [ -d "$FWD" ]; then
  ( cd "$FWD" && rm -rf amdgpu radeon i915 xe nvidia nouveau amdtee amd-ucode ath9k_htc ath10k ath11k ath12k ath6k \
      ar3k ath3k* iwlwifi-* rtw88 rtw89 rtlwifi rtl_bt brcm/brcmfmac* brcm/BCM* mediatek/mt7* mediatek/mt6* \
      ti-connectivity qcom qca cirrus intel/ibt-* intel/ipu* intel/vsc intel/avs intel/catpt intel/fw_sst* \
      mrvl/mwl* mrvl/sd* mrvl/pcie* mrvl/usb* mrvl/uap* libertas cypress vpu* dvb* s5p-* imx/sdma 2>/dev/null )
  echo "   firmwares gardes : $(du -sh "$FWD" | cut -f1)"
fi


say "Reglages de l'image"
# chaque switch aura ses propres cles SSH et son identifiant (crees au premier demarrage)
rm -f "$R"/etc/ssh/ssh_host_*
: > "$R/etc/machine-id"; rm -f "$R/var/lib/dbus/machine-id"
printf '# TL-SwitchOS : systeme en lecture seule (squashfs), voir tlswitch-image\n' > "$R/etc/fstab"
# noyau : ne change qu'avec une nouvelle image (il est lu par GRUB dans le squashfs)
dans apt-mark hold "$NOYAU" >/dev/null 2>&1 || true
K=$(ls "$R"/boot/vmlinuz-* | sort -V | tail -n 1); KV=${K##*/vmlinuz-}
dans update-initramfs -u -k "$KV" >/dev/null
rm -f "$R/boot/vmlinuz" "$R/boot/initrd.img" "$R/vmlinuz" "$R/initrd.img" "$R/vmlinuz.old" "$R/initrd.img.old" "$R/boot/vmlinuz.old" "$R/boot/initrd.img.old"
cp "$R/boot/vmlinuz-$KV" "$R/boot/vmlinuz"; cp "$R/boot/initrd.img-$KV" "$R/boot/initrd.img"
echo "   noyau $KV, initrd $(du -h "$R/boot/initrd.img" | cut -f1)"

say "GRUB (BIOS et UEFI, sans montage : marche aussi dans un conteneur LXC)"
E="$W/esp"; G="$E/boot/grub"; install -d "$G" "$E/EFI/BOOT"
FATID=$(od -An -N4 -tx4 /dev/urandom | tr -d ' ' | tr a-f A-F)
FATUUID="${FATID:0:4}-${FATID:4:4}"
cat > "$R/tmp/debut.cfg" <<EOF
search --no-floppy --fs-uuid --set=root $FATUUID
set prefix=(\$root)/boot/grub
EOF
MODS="part_msdos fat search search_fs_uuid normal configfile echo test regexp loadenv squash4 ext2 gzio linux probe"
if [ "$ARCH" = amd64 ]; then CIBLES="i386-pc x86_64-efi"; else CIBLES="arm64-efi"; fi
for c in $CIBLES; do
  install -d "$G/$c"; cp "$R/usr/lib/grub/$c/"*.mod "$R/usr/lib/grub/$c/"*.lst "$G/$c/"
  case $c in
    i386-pc)    dans grub-mkimage -O i386-pc -d /usr/lib/grub/i386-pc -c /tmp/debut.cfg -p /boot/grub \
                  -o /tmp/core.img biosdisk $MODS
                cp "$R/usr/lib/grub/i386-pc/boot.img" "$W/boot.img"; mv "$R/tmp/core.img" "$W/core.img" ;;
    x86_64-efi) dans grub-mkimage -O x86_64-efi -d /usr/lib/grub/x86_64-efi -c /tmp/debut.cfg -p /boot/grub \
                  -o /tmp/BOOTX64.EFI $MODS
                mv "$R/tmp/BOOTX64.EFI" "$E/EFI/BOOT/" ;;
    arm64-efi)  dans grub-mkimage -O arm64-efi -d /usr/lib/grub/arm64-efi -c /tmp/debut.cfg -p /boot/grub \
                  -o /tmp/BOOTAA64.EFI $MODS
                mv "$R/tmp/BOOTAA64.EFI" "$E/EFI/BOOT/" ;;
  esac
done
rm -f "$R/tmp/debut.cfg"
# live CD : memes GRUB, mais ils cherchent le CD / la cle (fichier repere unique)
if [ -z "$CARTE" ]; then
  LIVEID="tlslive-$IMAGE_ID"
  printf 'search --no-floppy --file --set=root /live/%s\nset prefix=($root)/boot/grub\n' "$LIVEID" > "$R/tmp/live.cfg"
  LMODS="$MODS iso9660 part_gpt search_fs_file"
  install -d "$W/live-efi/EFI/BOOT"
  if [ "$ARCH" = amd64 ]; then
    dans grub-mkimage -O i386-pc-eltorito -d /usr/lib/grub/i386-pc -c /tmp/live.cfg -p /boot/grub \
      -o /tmp/eltorito.img biosdisk $LMODS
    mv "$R/tmp/eltorito.img" "$W/eltorito.img"
    cp "$R/usr/lib/grub/i386-pc/boot_hybrid.img" "$W/boot_hybrid.img"
    dans grub-mkimage -O x86_64-efi -d /usr/lib/grub/x86_64-efi -c /tmp/live.cfg -p /boot/grub \
      -o /tmp/live.efi $LMODS
    mv "$R/tmp/live.efi" "$W/live-efi/EFI/BOOT/BOOTX64.EFI"
  else
    dans grub-mkimage -O arm64-efi -d /usr/lib/grub/arm64-efi -c /tmp/live.cfg -p /boot/grub \
      -o /tmp/live.efi $LMODS
    mv "$R/tmp/live.efi" "$W/live-efi/EFI/BOOT/BOOTAA64.EFI"
  fi
  rm -f "$R/tmp/live.cfg"
fi
SERIE=""; [ "$ARCH" = amd64 ] && SERIE='serial --unit=0 --speed=115200
terminal_input console serial
terminal_output console serial'
cat > "$G/grub.cfg" <<EOF
# TL-SwitchOS $VERSION - image compacte (systemes A et B). Fichier ecrit par
# construire-image.sh : le systeme a demarrer est choisi par grubenv (actif / essai).
set timeout=3
set default=0
insmod part_msdos
insmod fat
insmod squash4
insmod gzio
insmod regexp
$SERIE
regexp --set=1:tlsdisque '^([^,]+),' "\$root"
load_env
if [ -z "\$actif" ]; then set actif=2; fi
set slot=\$actif
# essai d'une nouvelle image : UNE seule fois. Si elle ne demarre pas, le switch
# redemarre (chien de garde, panic) et revient au systeme actif.
if [ -n "\$essai" ]; then set slot=\$essai; set essai=; save_env essai; fi
if [ "\$slot" = 3 ]; then set autre=2; else set autre=3; fi
probe --set=tlsdata --part-uuid (\$tlsdisque,msdos4)
set tlsopt="boot=tlswitch tlsdata=\$tlsdata panic=10 $KCMD net.ifnames=1"
# verrou : modifier le demarrage demande le compte admin du switch (pose par TL-SwitchOS)
if [ -f \$prefix/mdp.cfg ]; then source \$prefix/mdp.cfg; fi

menuentry "TL-SwitchOS" --unrestricted {
  linux (\$tlsdisque,msdos\$slot)/boot/vmlinuz \$tlsopt tlsslot=\$slot
  initrd (\$tlsdisque,msdos\$slot)/boot/initrd.img
}
if [ -f (\$tlsdisque,msdos\$autre)/boot/vmlinuz ]; then
  menuentry "TL-SwitchOS - systeme precedent" {
    linux (\$tlsdisque,msdos\$autre)/boot/vmlinuz \$tlsopt tlsslot=\$autre
    initrd (\$tlsdisque,msdos\$autre)/boot/initrd.img
  }
fi
menuentry "Copier TL-SwitchOS sur un disque interne" {
  linux (\$tlsdisque,msdos\$slot)/boot/vmlinuz \$tlsopt tlsslot=\$slot tlscopier
  initrd (\$tlsdisque,msdos\$slot)/boot/initrd.img
}
menuentry "Retour a la configuration d'usine" {
  linux (\$tlsdisque,msdos\$slot)/boot/vmlinuz \$tlsopt tlsslot=\$slot tlsreset
  initrd (\$tlsdisque,msdos\$slot)/boot/initrd.img
}
EOF
# grubenv : bloc de 1024 octets (comme grub-editenv create)
python3 -c "
import sys; t=b'# GRUB Environment Block\nactif=2\n'
sys.stdout.buffer.write(t + b'#' * (1024 - len(t)))" > "$G/grubenv"
cat > "$E/tlswitch.env" <<EOF
# TL-SwitchOS - reglages lus au PREMIER demarrage (modifiable depuis Windows,
# la partition TLSBOOT est en FAT). Ensuite tout se regle en CLI ou sur le web.
# Lignes sans valeur = valeur par defaut.
TLS_HOSTNAME=${TLS_HOSTNAME:-tl-switch}
# VLAN de gestion (1 = tous les ports au depart)
TLS_MGMT_VLAN=${TLS_MGMT_VLAN:-1}
# adresse fixe (ex : 192.168.1.30/24) ou dhcp
TLS_IP=${TLS_IP:-dhcp}
TLS_GW=${TLS_GW:-}
TLS_DNS=${TLS_DNS:-}
# mot de passe admin (efface du fichier une fois pose). Vide : admin / admin a changer
TLS_ADMIN_PASS=
# console serie
TLS_SERIAL=ttyS0
TLS_BAUD=115200
EOF
sed -i 's/$/\r/' "$E/tlswitch.env"      # lisible dans le Bloc-notes

if [ -n "$CARTE" ]; then
  say "U-Boot de la carte $CARTE"
  dans apt-get install -y -qq u-boot-rockchip u-boot-sunxi >/dev/null 2>&1 || true
  UB="$R/usr/lib/u-boot/$CARTE"
  if [ -f "$UB/u-boot-rockchip.bin" ]; then cp "$UB/u-boot-rockchip.bin" "$W/uboot.bin"; UBOFF=32768
  elif [ -f "$UB/u-boot-sunxi-with-spl.bin" ]; then cp "$UB/u-boot-sunxi-with-spl.bin" "$W/uboot.bin"; UBOFF=8192
  else echo "   U-Boot introuvable pour $CARTE (voir /usr/lib/u-boot/ dans u-boot-rockchip ou u-boot-sunxi)"; exit 1; fi
  dans apt-get purge -y -qq u-boot-rockchip u-boot-sunxi >/dev/null 2>&1 || true
fi

say "Nettoyage avant compression"
# GRUB est sur TLSBOOT : seul grub-editenv (grub-common) reste utile dans le systeme
# shellcheck disable=SC2086
dans apt-get purge -y -qq $GRUB_BIN >/dev/null 2>&1 || true
dans apt-get clean
rm -rf "$R"/var/lib/apt/lists/* "$R"/var/cache/apt/*.bin "$R"/var/log/*.log "$R"/var/log/apt/* \
       "$R"/usr/share/doc/*/[!c]* "$R"/usr/share/man/* "$R"/usr/share/info/* "$R"/tmp/* \
       "$R"/boot/vmlinuz-* "$R"/boot/initrd.img-* "$R"/boot/System.map-* "$R"/boot/config-*
find "$R/usr/share/locale" -mindepth 1 -maxdepth 1 ! -name fr ! -name locale.alias -exec rm -rf {} + 2>/dev/null || true
# index apt compresses (place sur TLSDATA pour les mises a jour de securite)
echo 'Acquire::GzipIndexes "true"; Acquire::CompressionTypes::Order:: "gz";' > "$R/etc/apt/apt.conf.d/91-tlswitch-index"
rm -f "$R/usr/sbin/policy-rc.d" "$R/etc/resolv.conf"
for m in dev/pts dev proc sys run; do umount -l "$R/$m"; done

say "Systeme compresse (squashfs xz)"
mksquashfs "$R" "$W/systeme.squashfs" -comp xz -Xbcj $BCJ -b 1M -noappend -quiet >/dev/null
T=$(stat -c %s "$W/systeme.squashfs")
echo "   systeme : $((T / 1048576)) Mo (slot : $SLOT_MO Mo)"
[ "$T" -le $((SLOT_MO * 1048576)) ] || { echo "systeme trop gros pour le slot : SLOT_MO plus grand"; exit 1; }

say "Disque ($((16 + BOOT_MO + 2 * SLOT_MO + DATA_MO)) Mo)"
rm -f "$OUT"; truncate -s $((16 + BOOT_MO + 2 * SLOT_MO + DATA_MO))M "$OUT"
sfdisk -q "$OUT" <<EOF
label: dos
start=16MiB, size=${BOOT_MO}MiB, type=ef, bootable
start=$((16 + BOOT_MO))MiB, size=${SLOT_MO}MiB, type=83
start=$((16 + BOOT_MO + SLOT_MO))MiB, size=${SLOT_MO}MiB, type=83
start=$((16 + BOOT_MO + 2 * SLOT_MO))MiB, type=83
EOF
placer() { dd if="$1" of="$OUT" bs=1M seek="$2" conv=notrunc status=none; }
truncate -s ${BOOT_MO}M "$W/esp.img"
mkfs.vfat -F 32 -s 1 -n TLSBOOT -i "$FATID" "$W/esp.img" >/dev/null
MTOOLS_SKIP_CHECK=1 mcopy -s -i "$W/esp.img" "$E"/* ::/
truncate -s ${DATA_MO}M "$W/data.img"
mkfs.ext4 -q -F -L TLSDATA "$W/data.img"
placer "$W/esp.img" 16
placer "$W/systeme.squashfs" $((16 + BOOT_MO))
placer "$W/data.img" $((16 + BOOT_MO + 2 * SLOT_MO))
if [ "$ARCH" = amd64 ]; then
  # demarrage BIOS (comme grub-bios-setup) : boot.img dans le MBR, core.img juste apres
  python3 - "$OUT" "$W/boot.img" "$W/core.img" <<'FINPY'
import struct, sys
disque, boot, core = sys.argv[1:]
b = bytearray(open(boot, "rb").read()[:440])
b[0x66:0x68] = b"\x90\x90"                    # disque de demarrage quelconque (cle USB, CF...)
c = bytearray(open(core, "rb").read())
c += b"\0" * (-len(c) % 512)
n = len(c) // 512
assert n < 32000, "core.img trop gros"
struct.pack_into("<QH", c, 0x1F4, 2, n - 1)   # liste de blocs de diskboot : secteurs 2 a n
with open(disque, "r+b") as f:
    f.write(b)
    f.seek(512); f.write(c)
FINPY
fi
if [ -n "$CARTE" ]; then
  dd if="$W/uboot.bin" of="$OUT" bs=1 seek="$UBOFF" conv=notrunc status=none
  echo "   U-Boot de $CARTE ecrit au debut de l'image"
fi

if [ -z "$CARTE" ]; then
  say "Live CD ($NOM-live.iso)"
  L="$W/iso"; SOCLE_MO=$((16 + BOOT_MO))
  install -d "$L/live" "$L/boot/grub"
  cp -r "$G/grub.cfg" "$L/boot/grub/grub.cfg"
  for c in $CIBLES; do cp -r "$G/$c" "$L/boot/grub/"; done
  cp "$R/boot/vmlinuz" "$L/live/vmlinuz"; cp "$R/boot/initrd.img" "$L/live/initrd.img"
  ln "$W/systeme.squashfs" "$L/live/systeme.squashfs" 2>/dev/null || cp "$W/systeme.squashfs" "$L/live/"
  # debut du disque installe : MBR, GRUB, partitions, TLSBOOT (le reste est ecrit par l'installation)
  dd if="$OUT" bs=1M count=$SOCLE_MO status=none | xz -T0 -9 > "$L/live/socle.img.xz"
  cat > "$L/live/disque.conf" <<EOF
SOCLE_MO=$SOCLE_MO
SLOT_A_MO=$((16 + BOOT_MO))
SLOT_B_MO=$((16 + BOOT_MO + SLOT_MO))
DATA_DEBUT_MO=$((16 + BOOT_MO + 2 * SLOT_MO))
EOF
  echo "$IMAGE_ID" > "$L/live/$LIVEID"
  cp "$E/tlswitch.env" "$L/tlswitch.env"
  cat > "$L/boot/grub/grub.cfg" <<EOF
# TL-SwitchOS $VERSION - live CD (CD ou cle USB)
set timeout=5
set default=0
$SERIE
set tlsopt="boot=tlswitch tlslive $KCMD net.ifnames=1"

menuentry "TL-SwitchOS live (configuration en memoire, rien n'est ecrit)" {
  linux /live/vmlinuz \$tlsopt
  initrd /live/initrd.img
}
menuentry "TL-SwitchOS live, copie en memoire (le CD / la cle peut etre retire)" {
  linux /live/vmlinuz \$tlsopt tlsram
  initrd /live/initrd.img
}
menuentry "Installer TL-SwitchOS sur un disque interne" {
  linux /live/vmlinuz \$tlsopt tlscopier
  initrd /live/initrd.img
}
EOF
  # partition EFI de l'ISO (FAT dans un fichier, sans montage)
  T=$(du -sk "$W/live-efi" | cut -f1)
  rm -f "$L/efi.img"; mkfs.vfat -C -n TLSEFI "$L/efi.img" $(( (T + 1024) / 1024 * 1024 )) >/dev/null
  MTOOLS_SKIP_CHECK=1 mcopy -s -i "$L/efi.img" "$W/live-efi"/* ::/
  cp -r "$W/live-efi/EFI" "$L/"        # copie lisible (cles USB preparees sous Windows)
  ISO="$HERE/$NOM-live.iso"; rm -f "$ISO"
  if [ "$ARCH" = amd64 ]; then
    cp "$W/eltorito.img" "$L/boot/grub/i386-pc/eltorito.img"
    xorriso -as mkisofs -r -J -V TLSLIVE -o "$ISO" \
      -b boot/grub/i386-pc/eltorito.img -no-emul-boot -boot-load-size 4 -boot-info-table \
      --grub2-boot-info --grub2-mbr "$W/boot_hybrid.img" \
      --efi-boot efi.img -efi-boot-part --efi-boot-image --protective-msdos-label "$L" 2>&1 \
      | grep -viE '^xorriso : (UPDATE|NOTE)|^Drive current|^Media current|^Media status|^Media summary|^Added to ISO|^libisofs|^ISO image produced|^Written to medium|^Writing to' || true
  else
    xorriso -as mkisofs -r -J -V TLSLIVE -o "$ISO" \
      --efi-boot efi.img -efi-boot-part --efi-boot-image --protective-msdos-label "$L" 2>&1 \
      | grep -viE '^xorriso : (UPDATE|NOTE)|^Drive current|^Media current|^Media status|^Media summary|^Added to ISO|^libisofs|^ISO image produced|^Written to medium|^Writing to' || true
  fi
  [ -s "$ISO" ] || { echo "ISO non produite"; exit 1; }
  echo "   live CD : $(( $(stat -c %s "$ISO") / 1048576 )) Mo"
fi

say "Mise a jour du systeme ($NOM.tlsi)"
SUM=$(sha256sum "$W/systeme.squashfs" | cut -d' ' -f1)
TLS_V="$VERSION" TLS_A="$ARCH" TLS_I="$IMAGE_ID" TLS_S="$SUM" TLS_T="$T" python3 - > "$W/MANIFEST.json" <<'PY'
import json, os, time
print(json.dumps({"produit": "TL-SwitchOS", "type": "image", "version": os.environ["TLS_V"],
                  "arch": os.environ["TLS_A"], "image": os.environ["TLS_I"],
                  "sha256": os.environ["TLS_S"], "taille": int(os.environ["TLS_T"]),
                  "date": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
PY
openssl pkeyutl -sign -inkey "$HERE/cles/maj-privee.pem" -rawin -in "$W/MANIFEST.json" -out "$W/MANIFEST.sig"
tar cf "$HERE/$NOM.tlsi" --owner=0 --group=0 -C "$W" MANIFEST.json MANIFEST.sig systeme.squashfs

say "Compression de l'image"
xz -T0 -6 -f "$OUT"
(cd "$HERE" && sha256sum "$NOM.img.xz" "$NOM.tlsi" $([ -z "$CARTE" ] && echo "$NOM-live.iso") | tee "$NOM.sha256")
cat <<EOF

Image prete : $OUT.xz
 1. Ecris-la sur la cle USB, la carte CF, le SATA DOM ou la carte SD :
      Windows : balenaEtcher ou Rufus (mode DD), directement le .img.xz
      Linux   : xzcat $NOM.img.xz | dd of=/dev/sdX bs=4M conv=fsync status=progress
    Petite machine sans port USB de demarrage ? Ecris-la directement sur son disque.
 2. Facultatif : regle le nom, le VLAN et l'IP dans TLSBOOT\\tlswitch.env.
 3. Demarre. Au premier demarrage le switch detecte ses ports, applique les reglages
    et redemarre une fois. L'adresse s'affiche ensuite a l'ecran (admin / admin).
 4. Demarre sur une cle et veux installer sur le disque interne : menu GRUB
    "Copier TL-SwitchOS sur un disque interne".
Mise a jour des switchs en image compacte : install image usbflash0:$NOM.tlsi
EOF
[ -n "$CARTE" ] || cat <<EOF

Live CD : $HERE/$NOM-live.iso (CD, ou cle USB : Rufus en mode DD, Etcher, Ventoy, dd)
  Demarre TL-SwitchOS sans rien ecrire sur la machine (configuration en memoire).
  Pour l'installer : menu "Installer TL-SwitchOS sur un disque interne",
  ou depuis le live : install image disk (la configuration faite en live suit).
EOF

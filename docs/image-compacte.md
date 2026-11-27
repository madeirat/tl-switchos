# TL-SwitchOS : image compacte (petites machines)

Depuis la 1.1.0, TL-SwitchOS existe aussi sous forme d'**image disque prête à écrire**, pour les machines trop petites pour l'ISO classique (qui demande 2 Go de RAM et 10 Go de disque).

| | ISO classique | Image compacte |
|---|---|---|
| RAM | 2 Go | **512 Mo** |
| Disque | 10 Go | **1 Go** (CF, SATA DOM, eMMC, clé USB, carte SD) |
| Installation | installateur Debian, Internet obligatoire | écrire l'image, démarrer, sans Internet |
| Système | Debian modifiable | squashfs en lecture seule + couche modifiable |
| Mise à jour du système | `install update`, `.tlsu` majeur | `install image` (A/B, retour automatique) |
| Processeur | x86_64 | x86_64 et **arm64** |

## Fabriquer l'image

Sur une Debian 13 reliée à Internet (le LXC de construction convient, aucun montage n'est nécessaire), en root, dans le dossier `tl-switchos` :

```bash
bash construire-image.sh                     # x86_64, BIOS et UEFI
bash construire-image.sh arm64               # ARM 64 bits (UEFI)
bash construire-image.sh arm64 --carte nanopi-r4s-rk3399
```

Résultat :

- `TL-SwitchOS-<version>-<arch>.img.xz` : l'image complète (environ 750 Mo une fois décompressée) ;
- `TL-SwitchOS-<version>-<arch>.tlsi` : le système seul, signé, pour mettre à jour les switchs déjà en image compacte ;
- `TL-SwitchOS-<version>-<arch>-live.iso` : le live CD (voir plus bas) ;
- `TL-SwitchOS-<version>-<arch>.sha256`.

Réglages possibles : `SLOT_MO=300` (taille d'un emplacement système), `DATA_MO=64` (taille de départ des données), `TLS_HOSTNAME`, `TLS_MGMT_VLAN`, `TLS_IP`, `TLS_GW`, `TLS_DNS` (écrits dans `tlswitch.env`).

## Organisation du disque

```
| 16 Mo libres | TLSBOOT 64 Mo | système A 300 Mo | système B 300 Mo | TLSDATA (le reste) |
   U-Boot ARM     FAT : GRUB,      squashfs           squashfs          ext4 : réglages,
                  grubenv,         (lecture seule)    (vide au départ)  couche modifiable
                  tlswitch.env
```

- **TLSBOOT** est en FAT : on peut ouvrir `tlswitch.env` depuis Windows avant le premier démarrage. Le mot de passe éventuel y est effacé dès qu'il est posé.
- **TLSDATA** est agrandie au premier démarrage jusqu'à la fin du disque.
- Le système lui-même n'est jamais modifié. Ce qui change (configuration, journaux, correctifs Debian) va dans la couche modifiable sur TLSDATA.

## Premier démarrage

1. Le switch agrandit TLSDATA, crée ses clés SSH et son certificat HTTPS.
2. Il détecte ses ports dans l'ordre matériel (`Gi1/0/1` à `Gi1/0/N`) et écrit la configuration de départ d'après `tlswitch.env` (par défaut : DHCP sur le VLAN 1, `admin` / `admin` à changer à la première connexion).
3. L'adresse s'affiche sur l'écran de connexion, comme avec l'ISO.

**Machine sans port USB de démarrage, ou disque interne non amovible** : démarrer sur une clé USB qui porte l'image, puis choisir dans le menu GRUB **Copier TL-SwitchOS sur un disque interne**. Le switch liste les disques, demande lequel, fait taper `EFFACER`, copie le système, puis il suffit de retirer la clé et de redémarrer.

## Live CD

`TL-SwitchOS-<version>-<arch>-live.iso` est une ISO hybride (environ 130 Mo) : on la grave sur un CD, ou on l'écrit sur une clé USB (Rufus en mode DD, balenaEtcher, Ventoy, `dd`). Elle démarre en BIOS et en UEFI.

Menu :

- **TL-SwitchOS live** : le switch tourne avec ses vrais ports, mais rien n'est écrit sur la machine. La configuration est en mémoire et disparaît à l'arrêt. Pratique pour essayer une machine avant de l'installer, ou pour un dépannage.
- **TL-SwitchOS live, copie en mémoire** : le système est chargé en RAM (environ 130 Mo en plus), le CD ou la clé peut être retiré.
- **Installer TL-SwitchOS sur un disque interne** : liste les disques, fait taper `EFFACER`, écrit TL-SwitchOS (même disque que l'image compacte : systèmes A/B, TLSDATA sur tout le reste).

Depuis le live, `install image disk` fait la même installation, **avec la configuration faite en live** (ports, VLAN, IP, mots de passe). `show version` et `show install image` indiquent qu'on est en live. `install image` (mise à jour) y est refusé.

Comme l'image compacte, le live demande **au moins 2 ports Ethernet** (sinon il le dit à l'écran).

Les réglages de départ du live sont dans `tlswitch.env` à la racine de l'ISO (modifiable avant de la graver, par exemple avec ISO Workshop ou `xorriso`).

## Mettre à jour

```
install image usbflash0:TL-SwitchOS-1.2.0-amd64.tlsi
install image http://192.168.1.5/TL-SwitchOS-1.2.0-amd64.tlsi
show install image
install image rollback
```

- Le fichier est lu au fil de l'eau (rien n'est copié sur TLSDATA, trop petite pour une image) et vérifié : signature, architecture, taille, somme SHA256.
- Le nouveau système s'écrit dans l'emplacement libre. Le switch redémarre **une seule fois** dessus. S'il démarre et applique sa configuration, il devient le système par défaut. Sinon (panique, blocage, chien de garde), le redémarrage suivant revient tout seul à l'ancien système.
- La configuration (`/etc/tlswitch`), les clés SSH, le nom des ports et l'historique sont conservés. Le reste de la couche modifiable repart à zéro pour ne pas masquer les fichiers du nouveau système.
- `install update` (dépôt APT) reste possible : il met à jour TL-SwitchOS et les correctifs de sécurité Debian dans la couche modifiable, sans toucher au noyau.

Retour à la configuration d'usine : entrée GRUB **Retour à la configuration d'usine** (protégée par le compte admin comme le reste du menu), ou `tlswitch-image usine` depuis `start-shell`.

## Cartes ARM

L'image arm64 démarre en UEFI : GRUB est sur TLSBOOT (`EFI/BOOT/BOOTAA64.EFI`). Ça marche directement avec les cartes qui ont un U-Boot récent en mémoire SPI ou un firmware EDK2 (NanoPi R5S/R6S avec EDK2, Raspberry Pi 4 avec le firmware UEFI, etc.). Pour une carte qui démarre uniquement sur carte SD, `--carte <nom>` écrit au début de l'image le U-Boot fourni par Debian (`u-boot-rockchip` ou `u-boot-sunxi`). Le nom est celui du dossier dans `/usr/lib/u-boot/`.

Les ports doivent être de vraies cartes réseau (une puce par port, ou un port par contrôleur USB/PCIe). Les cartes dont les ports passent par une petite puce switch interne (DSA, par exemple certaines BananaPi R) ne sont pas encore prises en charge.

## Ce qui a été testé

- Construction complète de l'image x86_64 dans un conteneur sans montage ni loop (Ubuntu 24.04 comme base de test : les miroirs Debian n'étaient pas joignables depuis l'environnement de construction ; la vraie image se construit sur Debian 13).
- Démarrage BIOS dans QEMU avec **512 Mo de RAM et un disque de 1 Go**, 3 cartes e1000 : menu GRUB, montage squashfs + couche modifiable, agrandissement de TLSDATA (64 Mo → 304 Mo), détection des ports, IP en DHCP, changement du mot de passe admin, verrou GRUB posé, `show version`, `show install image`, `show interfaces status`.
- Système de test : 123 Mo compressé avec un petit noyau. Avec le noyau Debian complet et les firmwares réseau, il faut compter environ 200 à 250 Mo, ce qui tient dans un emplacement de 300 Mo.
- Live CD dans QEMU avec **512 Mo de RAM** : en BIOS depuis un CD, et en UEFI depuis une clé USB (ISO écrite telle quelle). Ports détectés, mot de passe, `show version` et `show install image` en live ; copie en mémoire puis clé retirée, le switch continue de tourner ; `install image disk` sur un disque de 1 Go, puis redémarrage sur le disque avec la configuration faite en live (nom, mot de passe), verrou GRUB posé, TLSDATA agrandie.
- **Pas encore testé sur matériel réel** ni en UEFI, ni l'image arm64.

---

# Note : remplacer le firmware d'un vrai switch (ONIE)

TL-SwitchOS fait commuter les trames par le pont Linux. Sur un vrai switch, ça n'a d'intérêt que si la **puce de commutation** a un pilote dans le noyau Linux standard (switchdev) : le pont, les VLAN, LACP et STP sont alors **exécutés par la puce**, à pleine vitesse, et TL-SwitchOS n'a presque rien à changer (il utilise déjà un pont `vlan_filtering`, ce que switchdev accélère).

| Famille | Puce | Pilote dans Debian | Faisable |
|---|---|---|---|
| Mellanox / NVIDIA SN2010, SN2100, SN2410, SN2700 | Spectrum | `mlxsw` (firmware dans firmware-misc-nonfree) | **Oui**, la meilleure cible (occasion : quelques centaines d'euros) |
| Certains Marvell (Prestera 98DX) | Prestera | `prestera` | Possible, peu de modèles |
| Edgecore, Celestica, Delta, Quanta « Broadcom » | Trident, Tomahawk | non (SDK propriétaire) | Non : le trafic passerait par le processeur |
| TP-Link, Netgear, Zyxel GS1900, D-Link | Realtek RTL83xx/93xx | non (seulement OpenWrt, noyau à part) | Non sans tout refaire sur OpenWrt |
| Cisco, HP/Aruba, Juniper | propriétaires | non, firmware signé | Non |

Ce qu'il faudrait pour une version ONIE (switchs « whitebox ») :

1. Un **installateur ONIE** : script auto-extractible `onie-installer-x86_64` que l'ONIE du switch télécharge (HTTP, TFTP, USB) et lance. Il écrirait l'image compacte dans des partitions à côté de celle d'ONIE (sans y toucher) et ajouterait l'entrée GRUB.
2. Les ports de la puce (`swp1` à `swp32`, ports 100G avec éclatement 4×25G/4×10G) dans la détection des ports, avec la vitesse réelle dans `show interfaces status`.
3. Vérifier ce qui reste traité par le processeur : port security, storm control et DHCP snooping passent par nftables et ne sont pas accélérés par la puce.

Proposition : le faire dans une 1.2.0, en partant d'un Mellanox SN2010 ou SN2100 d'occasion comme matériel de test.

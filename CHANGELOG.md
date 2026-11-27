# Historique des versions

Numérotation **1.V.C** : V change avec les nouvelles fonctions, C avec les correctifs (sécurité et bugs). Une version corrective se met à jour sans rien changer à la configuration.

## 1.1.0

Nouvelles fonctions : **image compacte et live CD pour les petites machines**, **connexion SSO** à l'interface web, suffixe DNS, commandes `clear`.

- **Image compacte** (`construire-image.sh`) : TL-SwitchOS tient sur un disque de **1 Go** et tourne avec **512 Mo de RAM**. Pour les mini PC, clients légers, boîtiers type Streamcore (CF, SATA DOM, eMMC) et cartes ARM multiport.
  - Le système est compressé et en lecture seule (squashfs, environ 150 à 250 Mo) : une coupure de courant ne peut pas l'abîmer. Les réglages et la configuration sont sur une petite partition à part, agrandie toute seule au premier démarrage.
  - Deux emplacements de système (A et B) : une mise à jour s'écrit à côté du système actuel et n'est gardée que si elle démarre. Sinon le switch revient tout seul à l'ancien système.
  - Une seule image pour BIOS et UEFI. Elle s'écrit avec balenaEtcher, Rufus (mode DD) ou `dd`, sans installateur.
  - Premier démarrage sans question : détection des ports, IP en DHCP, `admin` / `admin`. Le nom, le VLAN, l'IP et le mot de passe peuvent être réglés avant, dans `tlswitch.env`, sur la partition TLSBOOT lisible depuis Windows.
  - Démarrage sur une clé USB, puis entrée GRUB **Copier TL-SwitchOS sur un disque interne** pour les boîtiers dont le disque n'est pas amovible.
  - Tous les pilotes Ethernet du noyau Debian et les firmwares réseau de la 1.0.6 ; graphique, son, Wi-Fi, Bluetooth et systèmes de fichiers de serveur retirés.
- **Live CD** (`TL-SwitchOS-<version>-<arch>-live.iso`, produit avec l'image) : une ISO hybride pour CD ou clé USB, BIOS et UEFI, d'environ 130 Mo. Elle démarre TL-SwitchOS sans rien écrire sur la machine (configuration en mémoire, perdue à l'arrêt), avec 512 Mo de RAM.
  - Entrée **copie en mémoire** : le système est chargé en RAM, le CD ou la clé peut être retiré.
  - Installation sur le disque interne depuis le menu, ou depuis le live avec `install image disk`. La configuration faite en live suit sur le disque.
- **ARM 64 bits** : `bash construire-image.sh arm64` pour les cartes qui démarrent en UEFI (U-Boot récent, EDK2) ; `--carte <nom>` écrit en plus le U-Boot Debian de la carte au début de l'image.
- Nouvelles commandes : `install image <source>` (nouveau système, fichier `.tlsi`, depuis une clé USB ou une URL), `install image rollback`, `show install image`. `show version` indique l'image et le système démarré.
- En image compacte, `install update` installe TL-SwitchOS et les correctifs Debian sans changer le noyau (il arrive avec une nouvelle image). Les mises à jour majeures `.tlsu` y sont refusées : c'est une nouvelle image qui fait passer à la version suivante de Debian.
- RADIUS : le module PAM est trouvé sur toutes les architectures (avant : x86_64 seulement).
- **Connexion SSO à l'interface web (OpenID Connect)** : Authentik, Keycloak, Entra ID… Se règle dans **Comptes > Connexion SSO** ou en CLI (`ip http sso issuer|client-id|client-secret|admin-group|read-group|label`). Les groupes de l'utilisateur donnent le niveau : administrateur (15) ou lecture seule (1). La page de connexion affiche un bouton « Se connecter avec … ». `show ip http sso` donne les adresses de retour à déclarer chez le fournisseur et teste qu'il répond. Les comptes locaux restent utilisables en secours.
- **Gestion centrale par TL-Manager** : `central enable` ouvre le port 2507 (mTLS), `central allow <adresse>` limite qui peut s'y connecter, `central accept <code>` valide l'appairage (code à 6 chiffres, aussi par un bouton dans **IP et système**), `show central`.
- **Suffixe DNS** : nouvelle commande `ip domain-name <suffixe>` (aussi dans **IP et système**). Sans elle, le suffixe donné par le DHCP est repris. Le certificat HTTPS de l'interface web contient le nom du switch, son nom complet `nom.suffixe` et ses adresses de gestion. Il est refait tout seul quand l'un d'eux change (y compris à un nouveau bail DHCP). Un certificat importé n'est jamais remplacé.
- **Commandes clear** : `clear counters` (tous les ports ou un seul), `clear arp-cache`, `clear lldp table`. `clear` efface aussi l'écran en mode configuration et dans la console de l'interface web. Les commandes `show` et `clear` marchent en mode configuration sans taper `do`. Bouton **Compteurs à zéro** dans la page Ports.
- **Installation dans un VLAN tagué** : quand le VLAN de gestion n'est pas 1, l'ISO demande s'il arrive tagué sur le port branché. Si oui, l'installateur Debian passe par ce VLAN (avant : il prenait une adresse dans un autre VLAN, ou aucune, et les dépôts Debian étaient injoignables), et ce port devient un trunk du VLAN de gestion. Même chose avec le script sur une Debian déjà installée qui sort par une interface VLAN (ex. `eno1.250`), ou avec `TLS_UPLINK=<port>`.
- **Debian installée sans miroir** : le script d'installation ajoute lui-même les dépôts Debian quand il n'y a que la ligne du CD, et retire cette ligne. Sans Internet, il dit ce qui manque (pas de route, ou DNS qui ne répond pas) au lieu d'une erreur d'apt.
- **Sécurité** (relecture de la 1.1.0) :
  - Session SSO : son niveau suit la config à chaque requête (groupe retiré ou changé : niveau revu ou session fermée) et elle dure 12 h au plus avant de repasser par le fournisseur.
  - SSO : le fournisseur doit annoncer exactement l'émetteur configuré.
  - Gestion centrale : la poignée de main TLS se fait dans le fil du client, un client muet ne bloque plus le port 2507.
  - Le suffixe DNS reçu du DHCP n'est repris que s'il est un nom DNS valide (un serveur DHCP pirate ne peut plus glisser d'autres noms dans le certificat).
  - `clear counters` demande le niveau 15, comme `clear arp-cache`.
- Interface web : nom du switch sur la page de connexion, mode d'accès (SSO) affiché à côté du compte. Le nom de l'éditeur n'apparaît plus dans le produit (`show version`, menus, certificat).

## 1.0.6

Correctif.

- **Démarrage verrouillé automatiquement** : le verrou se pose tout seul à la première connexion d'un admin local après la mise à jour (SSH, console ou web), sans retaper le mot de passe. Le switch ne garde que le mot de passe haché ; c'est à la connexion qu'il le connaît en clair, juste le temps de préparer celui de GRUB.
- **Plus de cartes réseau reconnues** : les firmwares des puces Ethernet courantes (Realtek, Broadcom, QLogic, Chelsio, Myricom, Netronome, Cavium, Intel) sont installés d'office, même pour une carte ajoutée plus tard. Le noyau Debian fournit déjà tous les pilotes. Sur une Debian installée à la main, il faut la section `non-free-firmware` dans les sources APT.

## 1.0.5

Correctifs de sécurité.

- **Démarrage verrouillé** : modifier une entrée de GRUB (touche `e`) ou ouvrir sa console demande le compte admin du switch et son mot de passe ; le mode de dépannage (recovery) est retiré. Le switch redémarre toujours seul. `show version` indique s'il est en place.
- RADIUS : une réponse sans Message-Authenticator valide est refusée (attaque « Blast-RADIUS »).
- Historique de la CLI : les lignes contenant un secret (mot de passe, clé, communauté) ne sont plus gardées, fichier lisible par son seul propriétaire ; les anciens historiques sont nettoyés à la mise à jour.
- Sauvegarde `copy … scp://` : adresse et nom vérifiés, plus d'option glissée dans l'URL.
- Mises à jour : un switch installé sans clé de signature la reçoit avec la mise à jour ; les paquets suivants doivent être signés.
- Export Prometheus : conseil affiché quand il est ouvert à tous (`prometheus exporter allow <réseau>`).

## 1.0.4

- **Installation** : le disque est toujours choisi à la main, y compris en console série. L'installateur demande quel disque utiliser et comment le partitionner (guidé ou manuel) ; rien n'est effacé sans confirmation. GRUB s'installe sur le disque choisi.
- Avant le partitionnement, l'installateur liste les disques avec leur taille et signale la clé d'installation, à ne pas choisir.
- Le mode sans question (`tlsauto`) garde le partitionnement automatique quand il n'y a qu'un seul disque.
- **Installation sur une Debian 13 déjà installée** : `curl -fsSLO https://deb.tech-linux.fr/tlswitchos/installer.sh && bash installer.sh`. Le script vérifie la clé du dépôt, prend la dernière version, pose les deux questions du switch et lance le même installateur que l'ISO.
- Commande `clear` (ou `clear screen`) pour effacer l'écran de la CLI.
- **Sécurité** :
  - `show running-config`, `show startup-config` et le téléchargement de la configuration sur le web demandent le niveau 15. Un compte en lecture ne voit plus les mots de passe hachés, la communauté SNMP ni la clé RADIUS.
  - Web : une session suit la configuration (compte supprimé = session fermée, niveau baissé = droits baissés tout de suite). Changer son mot de passe ferme ses autres sessions. Erreurs internes non détaillées au navigateur.
  - Web : plus d'en-tête HSTS (inadapté à un certificat auto-signé, Chrome refusait alors l'accès). Nouveau certificat avec SAN et usages, refait automatiquement s'il est d'un ancien modèle.
  - SSH : redirections de ports, d'agent et X11 interdites (un compte ne peut plus se servir du switch comme rebond vers les VLAN), connexions simultanées limitées.
  - Comptes : 8 caractères minimum, noms de comptes système Linux refusés, serveur NTP vérifié (adresse IP ou nom).
- Textes : « un VLAN » au masculin partout.
- Sans `mstpd`, l'avertissement « RSTP remplacé par STP classique » ne s'affiche plus qu'une fois par démarrage (et dans `show version`), au lieu de chaque commande.

## 1.0.3

- **SNMPv3** (SHA-256/512, AES) en plus de la communauté v2c, avec `snmp-server contact`. Un utilisateur retiré de la config ne reste pas valable dans snmpd.
- **Syslog distant** : `logging host` en UDP ou TCP, `logging trap <niveau>`. Les messages sont gardés en file d'attente si le serveur ne répond pas.
- **Connexion des administrateurs par RADIUS** (NPS de l'annuaire AD, FreeRADIUS) en SSH et sur le web : `radius-server host`, `aaa authentication login default group radius local`, comptes `username <nom> privilege 15 radius`, `test aaa group radius`. Le compte local reste toujours valable et n'est jamais envoyé au serveur RADIUS par le web.
- **Port security** : MAC statiques, nombre maximum, aging, violation `protect`, `restrict` ou `shutdown` avec état `err-disabled`, `clear errdisable interface`, `errdisable recovery interval`.
- **DHCP snooping** : réponses DHCP v4/v6 jetées sur les ports non `trust`, par VLAN si besoin.
- **IGMP snooping** (actif par défaut) et **querier**, `show ip igmp snooping groups`.
- **Storm control** broadcast et multicast en paquets par seconde.
- **Port mirroring (SPAN)** : `monitor session` en rx, tx ou les deux, jusqu'à 4 sessions.
- **Sauvegarde automatique** de la configuration en SCP (clé SSH propre au switch) : à chaque `write memory` et/ou périodiquement si la config a changé, historique horodaté sur le NAS et sur le switch, `show archive`, `archive config`.
- **Export Prometheus** sur le port 9101 (trafic, paquets, erreurs, rejets, état STP, MAC apprises, err-disabled par port), avec liste de réseaux autorisés. Lisible aussi par Zabbix.
- Correction : `username <nom> password-change-required` sur un compte inconnu plantait au lieu d'afficher l'erreur.
- Le paquet Debian dépend maintenant de `nftables`, `rsyslog` et `libpam-radius-auth`.
- Téléchargements : seule la dernière ISO reste sur le dépôt (les anciennes versions du paquet restent dans le dépôt APT et les Releases GitHub).

## 1.0.2

- **Dépôt APT TL-SwitchOS** : `show install updates` et `install update` en CLI, ou bouton **Rechercher les mises à jour** sur la page web Mise à jour. Une seule mise à jour couvre TL-SwitchOS et Debian, avec vérification, test et retour automatique comme pour les fichiers `.tlsu`.
- **Démarrage** : TL-SwitchOS n'attend plus le démarrage de l'interface web, de SNMP ou de RSTP. Avant, il restait bloqué 2 minutes, et juste après une mise à jour le secours remettait l'ancienne version.

## 1.0.1

Corrections issues de la première installation réelle.

- **DHCP sous VMware / ESXi** : le switch utilise l'adresse MAC de son premier port. Avant, le pont avait une MAC inventée et l'hyperviseur jetait les réponses DHCP : pas d'IP, pas de ping.
- **Démarrage** : le client DHCP ne bloque plus l'application de la configuration. Avant, l'IP et le mot de passe admin n'étaient pas appliqués et la connexion `admin`/`admin` était refusée.
- **Compte admin** : le mot de passe provisoire est posé dès l'installation. Root n'est verrouillé que si le compte admin fonctionne.
- **Écran de connexion** : l'adresse IP s'affiche avant le login et se met à jour en direct.
- **Installateur** : une IP fixe saisie à l'étape réseau Debian est bien reprise sur l'interface de gestion. La détection du disque fonctionne sans `awk`. La mise à l'heure ne peut plus bloquer plus de 15 s.
- `show ip interface brief` affiche « DHCP » pour une interface en DHCP, même avant l'obtention du bail.
- Bannière de la CLI : « Les connexions doivent se faire depuis un bastion ».

## 1.0.0

Première version : CLI, interface web, VLAN, LACP, RSTP, LLDP, SNMP, ISO d'installation et mises à jour signées.

# Historique des versions

## 1.0.4

- **Installation** : le disque est toujours choisi à la main, y compris en console série. L'installateur demande quel disque utiliser et comment le partitionner (guidé ou manuel) ; rien n'est effacé sans confirmation. GRUB s'installe sur le disque choisi.
- Le mode sans question (`tlsauto`) garde le partitionnement automatique quand il n'y a qu'un seul disque.

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

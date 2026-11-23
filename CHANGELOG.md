# Historique des versions

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

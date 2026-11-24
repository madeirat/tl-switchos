# Serveur de construction et dépôts (LXC)

Deux machines :

| Machine | Rôle | Expose |
|---|---|---|
| **construction** (LXC commun à tous les projets) | surveille GitHub, construit, **signe**, publie la Release, et sert les dépôts en lecture seule : dépôt APT, ISO, archive signée du site | HTTPS 8443 pour le reverse proxy, HTTP pour le serveur web, SSH depuis le bastion |
| **serveur web** (existant, Apache) | récupère l'archive du site, **vérifie sa signature**, la publie | le site, derrière le reverse proxy |

```
GitHub ──> construction ──HTTPS 8443──> reverse proxy ──> https://deb.<domaine>/<projet>/   (switchs, téléchargements)
             (clés)    └──HTTP (archive signée)──> serveur web ──> https://<projet>.<domaine>/
```

```
/srv/construction/projets/<projet>/depot   clone git
/srv/construction/projets/<projet>/cles    cles du projet (A SAUVEGARDER)
/srv/construction/projets/<projet>/cache   netinst Debian, site genere
/srv/depot/<projet>/                       depot servi par nginx (lecture seule)
/etc/construction/<projet>.conf            depot GitHub, jeton, branche, recette, adresse publique
```

nginx ne lit que `/srv/depot` : les clés et les jetons lui sont inaccessibles. Le serveur web ne détient aucun secret et refuse toute archive du site mal signée.

## 1. Créer le conteneur (hôte Proxmox)

Debian 13, non privilégié, avec `nesting=1` (cloisonnement systemd) et de la place pour les ISO.

```bash
pct create <id> local:vztmpl/debian-13-standard_<version>_amd64.tar.zst --hostname srv-depot \
  --cores 2 --memory 2048 --rootfs local-lvm:30 --unprivileged 1 --features nesting=1 --onboot 1 \
  --net0 name=eth0,bridge=vmbr0,ip=<ip>/24,gw=<passerelle>,firewall=1
```

Donne-lui une IP fixe. Puis crée le compte d'administration SSH (par défaut `sshadmin`) et sa clé avec l'outil de ton bastion.

## 2. Installer le serveur

```bash
pct push <id> installer-construction.sh /root/installer-construction.sh
pct exec <id> -- bash /root/installer-construction.sh
```

Questions : IP du bastion, compte SSH d'administration, IP autorisées en HTTPS 8443 (le reverse proxy), IP autorisées en HTTP (le serveur web, qui récupère le site). Les réponses sont gardées dans `/etc/construction/serveur.conf`. Pour les changer, modifie ce fichier puis relance le script.

## 3. Ajouter TL-SwitchOS

Crée d'abord un **jeton GitHub** : Settings → Developer settings → *Fine-grained tokens*. Donne-lui accès au seul dépôt TL-SwitchOS, avec la permission **Contents : Read and write** et une date d'expiration.

```bash
construction ajouter tlswitchos     # depot GitHub, jeton, branche, adresse publique du depot
construction lancer tlswitchos      # premiere construction (cree les cles), puis publication
construction sauvegarder tlswitchos # archive chiffree des cles, a ranger hors ligne
```

L'**adresse publique** (ex. `https://deb.exemple.fr/tlswitchos`) est écrite dans l'ISO : les switchs y chercheront leurs mises à jour. Fixe-la avant la première construction.

Pour la sauvegarde, sur l'hôte Proxmox : `pct pull <id> /root/cles-tlswitchos-<date>.tar.gz.gpg ./`. Range l'archive hors ligne, garde la phrase de passe ailleurs, puis efface l'archive du conteneur.

## 4. Publier le site sur le serveur web

Sur le serveur web Apache, après la première construction :

```bash
bash installer-site-web.sh
```

Questions : projet, domaine du site, adresse du dépôt du projet sur la construction (`http://<ip-construction>/tlswitchos`), IP du reverse proxy. Le script épingle la clé du dépôt après vérification de son empreinte, puis récupère le site toutes les 10 minutes. Il ne touche ni au pare-feu ni à SSH de la machine.

## 5. Reverse proxy

Deux sites, avec le certificat du domaine :

- `deb.<domaine>` → `https://<ip-construction>:8443` (certificat auto-signé : `proxy_ssl_verify off`) ;
- `<projet>.<domaine>` → le serveur web, comme ses autres sites.

## Utilisation

| Commande (sur la construction) | Rôle |
|---|---|
| `construction liste` | projets et état de leur minuterie |
| `construction lancer <projet>` | construire et publier maintenant |
| `construction journal <projet>` | suivre la dernière construction |
| `construction ajouter <projet>` | ajouter un projet (ou réparer un clone) |
| `construction sauvegarder <projet>` | archive chiffrée des clés du projet |

Sur le serveur web : `systemctl start site-depot@<projet>` (mise à jour immédiate), `journalctl -u site-depot@<projet>`.

**Publier une version de TL-SwitchOS** : change `VERSION` dans `tlswitch/tlswitch.py`, ajoute une section `## X.Y.Z` en tête de `CHANGELOG.md`, puis pousse sur la branche. Dans les 10 minutes, la Release, le dépôt APT et les téléchargements sont à jour, et le site 10 minutes plus tard au plus.

Une mise à jour **majeure** (montée de version de Debian) ne passe pas par le dépôt APT : `MAJEURE_DEBIAN=forky construction lancer tlswitchos` publie un `.tlsu`, à installer à la main.

## Ajouter un autre projet

Le projet fournit dans son dépôt une recette (script bash). La construction la lance avec les variables `DEPOT`, `CLES`, `CACHE`, `WWW` (dossier publié ensuite dans `/srv/depot/<projet>`), `DEPOT_URL`, `GH_REPO`, `GH_TOKEN` et `PUBLIER_PAGES`. Prends `outils/publier-release.sh` de TL-SwitchOS comme modèle, puis lance `construction ajouter <projet>`. La publication a lieu quand `dists/stable/InRelease` existe dans `WWW`.

## Sécurité

| Mesure | Détail |
|---|---|
| Pare-feu (nftables) | en entrée : SSH depuis le bastion, HTTPS 8443 depuis le proxy, HTTP depuis le serveur web, rien d'autre. En sortie : DNS, NTP, HTTP et HTTPS (GitHub, miroirs Debian). |
| SSH | clé uniquement, compte d'administration depuis le bastion seulement, root refusé, aucune redirection. |
| Constructions | lancées par l'utilisateur système `construction` (sans shell ni droits), dans un service systemd cloisonné : système en lecture seule, écriture limitée au dossier du projet et à son dépôt, pas d'élévation de privilèges, `/tmp` privé. `construction lancer` passe toujours par ce service, même lancé en root. |
| Clés | dossier `cles` en `700`, dans `/srv/construction/projets` (`750`) que nginx ne peut pas lire ; sauvegarde chiffrée en AES-256. |
| Jeton GitHub | `/etc/construction/<projet>.conf` (`640`, root et `construction`), donné à git par l'environnement, jamais écrit dans `.git/config`. |
| nginx | GET/HEAD seulement, liste des dossiers comme un miroir Debian (tout y est public et signé), version masquée, fichiers cachés refusés. |
| Site | archive signée avec la clé du dépôt, vérifiée par `gpgv` avec une clé épinglée. Liens, fichiers spéciaux et chemins `..` refusés. Récupération par un compte sans droits, dans un service cloisonné. Apache : proxy seulement, GET/HEAD, CSP stricte, HSTS. |
| Système | mises à jour de sécurité Debian automatiques (`unattended-upgrades`). Postfix et rpcbind arrêtés. |

**Côté GitHub**, protège la branche (Settings → Branches → *Branch protection rule*) : la construction signe ce qui arrive sur la branche, donc seules des personnes de confiance doivent pouvoir y pousser. Active aussi la double authentification sur les comptes qui y ont accès.

**Si le service refuse de démarrer** (`status=226/NAMESPACE` dans `construction journal <projet>`), le conteneur ne permet pas le cloisonnement : vérifie l'option `nesting=1`.

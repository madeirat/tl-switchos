#!/usr/bin/env python3
"""TL-SwitchOS - rubrique « TL-Manager » du site (console centrale), appelee par generer-site.py.

    import page_manager
    for nom, contenu in page_manager.pages(G, version, github, depot_url, tele, depot):
        ecrire(www, nom, contenu)

Trois pages, dans la rubrique TL-Manager du menu (sous-menu commun du gabarit) :
tl-manager.html (presentation et telechargements), tl-manager-installation.html,
tl-manager-documentation.html.
G est le module generer-site (gabarit, entete_doc, taille, cle_version, GITHUB_ICO).
Les fichiers lus dans <tele> : TL-Manager-<v>-amd64.iso et TL-Manager-<v>-lxc-amd64.tar.zst
(+ .sha256), publies a cote des ISO de TL-SwitchOS.
"""
import html
import os
import re
import time

FICHIERS = (("iso", r"^TL-Manager-([0-9.]+)-amd64\.iso$", "Image ISO · machine virtuelle ou physique"),
            ("lxc", r"^TL-Manager-([0-9.]+)-lxc-amd64\.tar\.zst$", "Modèle de conteneur LXC · Proxmox"))

FONCTIONS = [
    ("Assistant d'ajout", "L'adresse du switch, un code à 6 chiffres à valider sur le switch, et c'est géré."),
    ("Échange de clés", "TL-Manager et chaque switch ne se reconnaissent que par leurs clés (mTLS). Aucun mot de passe de switch stocké."),
    ("Tableau de bord", "Tous les switchs, en ligne ou non, versions, ports connectés et alarmes, rafraîchis toutes les 30 s."),
    ("Console CLI à distance", "La CLI de chaque switch depuis le navigateur, chaque commande tracée dans le journal."),
    ("Config groupée", "Les mêmes commandes poussées sur une sélection, un site ou un groupe, avec retour arrière automatique."),
    ("Historique des configs", "Une version gardée à chaque changement, comparaison de deux versions, téléchargement."),
    ("Mises à jour groupées", "Les switchs choisis se mettent à jour depuis le dépôt signé."),
    ("Topologie", "Les liens entre switchs déduits du LLDP, et les autres voisins découverts."),
    ("Comptes et journal", "Rôle admin ou lecture (console en niveau 1), journal de toutes les actions."),
]


def fichiers(dossier, cle_version):
    """{type: dernier fichier} pour l'ISO et le modele LXC."""
    res = {}
    if not os.path.isdir(dossier):
        return res
    for f in os.listdir(dossier):
        for typ, motif, _ in FICHIERS:
            m = re.match(motif, f)
            if not m:
                continue
            p = os.path.join(dossier, f)
            sha = ""
            try:
                sha = open(p + ".sha256").read().split()[0]
            except (OSError, IndexError):
                pass
            d = {"version": m.group(1), "fichier": f, "taille": os.path.getsize(p), "sha": sha,
                 "date": time.strftime("%d/%m/%Y", time.localtime(os.path.getmtime(p)))}
            if typ not in res or cle_version(d["version"]) > cle_version(res[typ]["version"]):
                res[typ] = d
    return res


def lien_github(G, github):
    if not github or "/" not in github:
        return ""
    url = "https://github.com/%s/tl-manager" % github.split("/")[0]
    return '<a class="btn" href="%s">%sCode source de TL-Manager</a>' % (html.escape(url), G.GITHUB_ICO)


def page_presentation(G, version, github, depot_url, tele):
    e = html.escape
    dl = depot_url.rstrip("/") + "/telechargements/"
    fs = fichiers(tele, G.cle_version)
    vm = next((f["version"] for f in fs.values()), "")
    cartes = []
    for typ, _, libelle in FICHIERS:
        f = fs.get(typ)
        if f:
            cartes.append(
                '<div class="dl-carte"><div><p class="dl-titre">TL-Manager {v}</p><p class="muet">{l} · {t}</p></div>'
                '<a class="btn primaire" href="{dl}{f}" download>Télécharger</a>'
                '<div class="sha"><span>SHA-256</span><code>{s}</code>'
                '<button class="copier" type="button" data-copie="{s}" aria-label="Copier le SHA-256">Copier</button></div>'
                '</div>'.format(v=e(f["version"]), l=e(libelle), t=G.taille(f["taille"]), dl=e(dl), f=e(f["fichier"]),
                                s=e(f["sha"] or "?")))
        else:
            cartes.append('<div class="dl-carte"><div><p class="dl-titre">TL-Manager {v}</p>'
                          '<p class="muet">{l} : bientôt disponible.</p></div></div>'.format(v=e(vm), l=e(libelle)))
    grille = "".join('<article class="carte"><h3>%s</h3><p>%s</p></article>' % (e(a), e(b)) for a, b in FONCTIONS)
    corps = G.entete_doc("TL-Manager", "La console centrale de vos switchs TL-SwitchOS : un serveur, et chaque switch "
                         "s'y rattache par un échange de clés.") + """
<div class="largeur doc">
<section class="doc-groupe"><h2>Ce que fait TL-Manager</h2><div class="grille">{grille}</div></section>
<section class="doc-groupe"><h2>Comment ça marche</h2>
<ol class="etapes">
  <li><span>1</span><h3>Installer</h3><p>TL-Manager en conteneur LXC ou en machine virtuelle.</p></li>
  <li><span>2</span><h3>Ouvrir</h3><p><code>central enable</code> sur chaque switch (port 2507).</p></li>
  <li><span>3</span><h3>Rattacher</h3><p>L'assistant, puis <code>central accept</code> avec le code affiché.</p></li>
</ol>
<p class="suite"><a href="tl-manager-installation.html">Guide d'installation →</a></p></section>
<section id="telecharger" class="doc-groupe"><h2>Télécharger</h2>{cartes}
<p class="muet">TL-Manager tourne sur Debian 13. Il faut des switchs en TL-SwitchOS 1.1.0 ou plus récent.</p>
<p class="actions">{gh}</p></section>
</div>""".format(grille=grille, cartes="".join(cartes), gh=lien_github(G, github))
    return G.gabarit("TL-Manager · TL-SwitchOS", "TL-Manager : console centrale des switchs TL-SwitchOS, en ISO ou en "
                     "conteneur LXC.", corps, version, github, "tl-manager.html")


def page_installation(G, version, github, tele):
    e = html.escape
    fs = fichiers(tele, G.cle_version)
    lxc = fs.get("lxc", {}).get("fichier", "TL-Manager-<version>-lxc-amd64.tar.zst")
    corps = G.entete_doc("Installer TL-Manager", "En conteneur LXC sur Proxmox, en machine virtuelle ou sur une "
                         "Debian 13 existante, puis rattacher les switchs.") + """
<div class="largeur doc texte">
<h2>Ce qu'il faut</h2>
<ul>
<li>Un conteneur ou une VM Debian 13 : 1 vCPU, 512 Mo de RAM (1 Go en VM), 4 à 8 Go de disque.</li>
<li>Une adresse IP fixe dans le réseau de gestion des switchs.</li>
<li>Le pare-feu doit laisser passer TL-Manager vers les switchs en <strong>TCP 2507</strong>, et les postes d'administration vers TL-Manager en <strong>HTTPS (443)</strong>.</li>
<li>Des switchs en TL-SwitchOS 1.1.0 ou plus récent.</li>
</ul>

<h2>En conteneur LXC (Proxmox)</h2>
<ol>
<li>Télécharge le modèle depuis la page <a href="tl-manager.html#telecharger">TL-Manager</a>, puis dépose-le dans <em>Stockage › CT Templates › Téléverser</em> (ou dans <code>/var/lib/vz/template/cache/</code>).</li>
<li>Crée le conteneur, avec l'interface de Proxmox (<em>Créer CT</em>) ou en ligne de commande :
<pre><code>pct create 120 local:vztmpl/{lxc} --hostname tl-manager \\
  --cores 1 --memory 512 --rootfs local-lvm:4 --unprivileged 1 --features nesting=1 --onboot 1 \\
  --net0 name=eth0,bridge=vmbr0,ip=192.0.2.10/24,gw=192.0.2.1 --password
pct start 120</code></pre></li>
<li>La console du conteneur affiche l'adresse de l'interface web et le compte <code>admin</code> provisoire.</li>
</ol>

<h2>En machine virtuelle (ISO)</h2>
<ol>
<li>Crée une VM : 1 vCPU, 1 Go de RAM, 8 Go de disque, une carte réseau dans le réseau de gestion.</li>
<li>Démarre sur l'ISO. L'installateur Debian, en français et en AZERTY, demande le nom de la machine, le réseau (DHCP ou IP fixe), le mot de passe root et le disque, puis installe TL-Manager.</li>
<li>Au redémarrage, l'écran affiche l'adresse web et le compte <code>admin</code> provisoire.</li>
</ol>

<h2>Sur une Debian 13 existante</h2>
<pre><code>git clone https://github.com/tech-linux-fr/tl-manager.git
cd tl-manager
bash installer-tl-manager.sh</code></pre>
<p>Le script peut être relancé sans risque : il sert aussi à mettre TL-Manager à jour, en gardant la base, les clés et les comptes.</p>

<h2>Première connexion</h2>
<ol>
<li>Ouvre <code>https://&lt;adresse de TL-Manager&gt;</code>. Le certificat est signé par l'autorité propre à TL-Manager : accepte-le la première fois.</li>
<li>Connecte-toi avec <code>admin</code> et le mot de passe provisoire affiché sur la console, puis choisis ton mot de passe (10 caractères minimum).</li>
<li>Les autres comptes se créent dans <em>Comptes</em> (rôle admin ou lecture), ou en ligne de commande : <code>tl-manager compte &lt;nom&gt; --role admin</code>.</li>
</ol>

<h2>Rattacher un switch</h2>
<ol>
<li>Sur le switch, en niveau 15 :
<pre><code>central enable
central allow 192.0.2.10</code></pre>
Le switch ouvre le port 2507, seulement pour l'adresse de TL-Manager.</li>
<li>Dans TL-Manager, <em>Ajouter un switch</em> : son adresse et le port 2507. TL-Manager affiche le nom, la version et l'empreinte de la clé du switch, à comparer avec <code>show central</code>.</li>
<li>TL-Manager affiche un code à 6 chiffres. Sur le switch :
<pre><code>central accept 123456</code></pre>
Le code peut aussi être validé dans l'interface web du switch : <em>IP et système › Gestion centrale</em>.</li>
<li>Donne au switch un nom, un site et un groupe : il apparaît dans le tableau de bord.</li>
</ol>
<p class="suite"><a href="tl-manager-documentation.html">Documentation →</a></p>
</div>""".format(lxc=e(lxc))
    return G.gabarit("Installation · TL-Manager", "Installer TL-Manager en LXC, en VM ou sur Debian 13, et y rattacher "
                     "les switchs TL-SwitchOS.", corps, version, github, "tl-manager-installation.html")


def page_documentation(G, version, github):
    corps = G.entete_doc("Documentation TL-Manager", "Tout ce que fait la console centrale, et comment s'en servir "
                         "au quotidien.") + """
<div class="largeur doc texte">
<h2>Tableau de bord</h2>
<p>La liste de tous les switchs gérés : état (en ligne, hors ligne, appairage en attente), site et groupe, version, ports connectés, uptime, dernier contact. TL-Manager interroge chaque switch toutes les 30 secondes. Un switch passe hors ligne après 75 secondes sans réponse.</p>
<p>Les <strong>alarmes</strong> signalent : switch injoignable (avec la raison), configuration non enregistrée, retour arrière en attente, ports err-disabled et mise à jour en échec.</p>

<h2>Fiche d'un switch</h2>
<ul>
<li><strong>Vue d'ensemble</strong> : ports (état, mode, VLAN, vitesse, description), VLAN, adresses de gestion, voisins LLDP.</li>
<li><strong>Console CLI</strong> : la CLI du switch dans le navigateur, avec <code>?</code> et l'historique des commandes. Un compte admin y travaille en niveau 15, un compte lecture en niveau 1 (commandes <code>show</code>). Chaque commande est inscrite au journal, et les lignes qui contiennent un secret sont masquées.</li>
<li><strong>Configuration</strong> : la running-config, avec une version gardée à chaque changement (200 par switch). Coche deux versions pour voir leurs différences.</li>
<li><strong>Actions</strong> : pousser des commandes, enregistrer la config (<code>write memory</code>), lancer la mise à jour depuis le dépôt, renommer, ranger, retirer le switch.</li>
</ul>

<h2>Pousser des commandes</h2>
<p>On écrit des commandes du mode configuration, une par ligne, comme après <code>conf t</code>. Elles sont appliquées dans l'ordre, et les erreurs sont listées ligne par ligne.</p>
<p>Si une commande touche l'accès de gestion du switch (IP, VLAN de gestion, ports qui le portent), le switch arme son retour arrière automatique. TL-Manager se reconnecte alors pour confirmer. Si le switch n'est plus joignable, il revient seul à la configuration précédente au bout de 2 minutes.</p>

<h2>Config groupée</h2>
<p>La même action sur plusieurs switchs : commandes, enregistrement ou mise à jour. La sélection se fait à la main, par site ou par groupe. Chaque switch devient une tâche, à suivre dans <em>Tâches</em>. Pour une mise à jour, commence par un seul switch.</p>

<h2>Topologie</h2>
<p>Les liens entre switchs gérés sont déduits du LLDP, avec les ports de chaque côté. Les autres voisins découverts (routeur, serveurs, bornes…) sont listés dessous.</p>

<h2>Comptes et journal</h2>
<ul>
<li><strong>admin</strong> : tout, y compris l'ajout de switchs, la configuration et les comptes.</li>
<li><strong>lecture</strong> : tableau de bord, fiches, topologie et console en niveau 1.</li>
</ul>
<p>Le journal garde les connexions, les commandes passées en console, les configurations poussées, les ajouts et retraits de switchs, et chaque changement de configuration détecté.</p>

<h2>Commandes du switch</h2>
<ul>
<li><code>show central</code> : état, port, adresses autorisées, empreinte du switch, TL-Manager appairé, dernier contact, demandes en attente.</li>
<li><code>central enable</code> : ouvre le port 2507 (<code>central enable port &lt;port&gt;</code> pour un autre port).</li>
<li><code>central allow &lt;adresse|réseau&gt;</code> : n'accepte que cette adresse. <code>central allow any</code> : toutes.</li>
<li><code>central accept &lt;code&gt;</code> : valide l'appairage avec le code affiché par TL-Manager.</li>
<li><code>central forget</code> : oublie le TL-Manager, qui ne peut plus gérer ce switch.</li>
<li><code>central disable</code> : ferme le port 2507, l'appairage est gardé.</li>
</ul>
<p>Ce sont des commandes du mode EXEC. Depuis le mode configuration, préfixe-les par <code>do</code>.</p>

<h2>Sécurité</h2>
<ul>
<li>Toutes les connexions sont chiffrées en TLS 1.2 ou plus.</li>
<li>L'appairage échange les clés : le switch garde le certificat de son TL-Manager, et TL-Manager garde l'empreinte de la clé du switch. Le code à 6 chiffres est calculé à partir des deux : un intermédiaire qui s'interposerait obtiendrait un autre code.</li>
<li>Ensuite, le switch refuse tout autre client, et TL-Manager refuse un switch dont la clé a changé.</li>
<li>Aucun mot de passe de switch n'est stocké dans TL-Manager.</li>
<li>Interface web : mot de passe provisoire à changer, sessions limitées (30 min d'inactivité), protection CSRF, blocage après 8 essais ratés.</li>
</ul>

<h2>Sauvegarde et fichiers</h2>
<ul>
<li>Données : <code>/var/lib/tl-manager</code> (base, autorité et clés). Une copie de la base est faite chaque nuit dans <code>sauvegardes/</code> et gardée 14 jours. Sauvegarde ce dossier avec le conteneur.</li>
<li>Réglages : <code>/etc/tl-manager/manager.conf</code> (<code>port_web</code>, <code>nom</code>). Service : <code>systemctl status tl-manager</code>.</li>
</ul>

<h2>Dépannage</h2>
<ul>
<li><strong>« injoignable » à l'ajout</strong> : sur le switch, vérifie <code>show central</code> (activée, port, adresses autorisées) et le pare-feu entre TL-Manager et le switch (TCP 2507).</li>
<li><strong>« le switch présente une autre clé »</strong> : le switch a été réinstallé, ou quelqu'un s'interpose. Si la réinstallation est voulue, retire le switch de TL-Manager et ajoute-le à nouveau.</li>
<li><strong>Code refusé sur le switch</strong> : la demande expire après 10 minutes. Relance l'étape « Échange de clés » dans l'assistant.</li>
<li><strong>Mot de passe admin perdu</strong> : sur TL-Manager, en root, <code>tl-manager compte admin</code>.</li>
</ul>
</div>"""
    return G.gabarit("Documentation · TL-Manager", "Documentation de TL-Manager : tableau de bord, console, config "
                     "groupée, sécurité, dépannage.", corps, version, github, "tl-manager-documentation.html")


def pages(G, version, github, depot_url, tele, depot=""):
    return [("tl-manager.html", page_presentation(G, version, github, depot_url, tele)),
            ("tl-manager-installation.html", page_installation(G, version, github, tele)),
            ("tl-manager-documentation.html", page_documentation(G, version, github))]

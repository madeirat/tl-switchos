#!/usr/bin/env python3
"""TL-SwitchOS - page « TL-Manager » du site (console centrale), appelee par generer-site.py.

    import page_manager
    ecrire(www, "tl-manager.html", page_manager.page(G, version, github, depot_url, tele, depot))

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


def version_manager(depot):
    """Version du clone de tech-linux-fr/tl-manager (TLM_SRC, pose par publier-release.sh)."""
    src = os.environ.get("TLM_SRC") or os.path.join(depot, "tl-manager")
    try:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(os.path.join(src, "tlmanager", "tlmanager.py")).read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def page(G, version, github, depot_url, tele, depot):
    e = html.escape
    dl = depot_url.rstrip("/") + "/telechargements/"
    fs = fichiers(tele, G.cle_version)
    vm = version_manager(depot)
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
    lxc = fs.get("lxc", {}).get("fichier", "TL-Manager-<version>-lxc-amd64.tar.zst")
    gh = ""
    if github and "/" in github:
        url = "https://github.com/%s/tl-manager" % github.split("/")[0]
        gh = '<p><a class="btn" href="%s">%sCode source de TL-Manager</a></p>' % (e(url), G.GITHUB_ICO)
    grille = "".join('<article class="carte"><h3>%s</h3><p>%s</p></article>' % (e(a), e(b)) for a, b in FONCTIONS)
    corps = G.entete_doc(
        "TL-Manager", "La console centrale de vos switchs TL-SwitchOS : un serveur, et chaque switch s'y rattache "
        "par un échange de clés.") + """
<div class="largeur doc">
<section class="doc-groupe"><h2>Ce que fait TL-Manager</h2><div class="grille">{grille}</div></section>
<section id="telecharger" class="doc-groupe"><h2>Télécharger</h2>{cartes}
<p class="muet">TL-Manager tourne sur Debian 13. Il faut un switch en TL-SwitchOS 1.1.0 ou plus récent.</p>{gh}</section>
</div>
<div class="largeur doc texte">
<h2>Installer TL-Manager</h2>
<h3>En conteneur LXC (Proxmox)</h3>
<ol>
<li>Dépose le modèle dans <em>Stockage › CT Templates › Téléverser</em> (ou dans <code>/var/lib/vz/template/cache/</code>).</li>
<li>Crée le conteneur dans le VLAN de gestion des switchs :
<pre><code>pct create 120 local:vztmpl/{lxc} --hostname tl-manager \\
  --cores 1 --memory 512 --rootfs local-lvm:4 --unprivileged 1 --features nesting=1 --onboot 1 \\
  --net0 name=eth0,bridge=vmbr0,ip=192.0.2.10/24,gw=192.0.2.1 --password
pct start 120</code></pre></li>
<li>La console du conteneur affiche l'adresse de l'interface web et le compte <code>admin</code> provisoire, à changer à la première connexion.</li>
</ol>
<h3>En machine virtuelle (ISO)</h3>
<p>1 vCPU, 1 Go de RAM, 8 Go de disque. L'installateur Debian, en français, demande le nom de la machine, le réseau, le mot de passe root et le disque, puis installe TL-Manager. Au redémarrage, l'écran affiche l'adresse web et le compte provisoire.</p>
<h3>Sur une Debian 13 existante</h3>
<pre><code>bash installer-tl-manager.sh</code></pre>
<p>Le script est dans le dépôt TL-Manager sur GitHub (lien ci-dessus).</p>

<h2>Ajouter un switch</h2>
<ol>
<li>Sur le switch, en niveau 15 : <pre><code>central enable
central allow 192.0.2.10</code></pre>Le switch ouvre le port <strong>2507</strong>, seulement pour l'adresse de TL-Manager.</li>
<li>Dans TL-Manager, <em>Ajouter un switch</em> : son adresse et le port 2507. TL-Manager affiche le nom, la version et l'empreinte de la clé du switch (à comparer avec <code>show central</code>).</li>
<li>TL-Manager affiche un <strong>code à 6 chiffres</strong>. Sur le switch : <pre><code>central accept 123456</code></pre>Le code est calculé à partir des deux clés : s'il ne correspond pas, quelqu'un s'est interposé.</li>
<li>Donne au switch un nom, un site et un groupe : il apparaît dans le tableau de bord.</li>
</ol>
<h2>Commandes du switch</h2>
<ul>
<li><code>show central</code> : état, empreinte du switch, TL-Manager appairé, dernier contact, demandes en attente.</li>
<li><code>central enable</code> / <code>central enable port &lt;port&gt;</code> / <code>central disable</code> : ouvrir ou fermer le port de gestion centrale.</li>
<li><code>central allow &lt;adresse|réseau&gt;</code> / <code>central allow any</code> : limiter qui peut se connecter.</li>
<li><code>central accept &lt;code&gt;</code> : valider l'appairage. <code>central forget</code> : couper le lien avec TL-Manager.</li>
</ul>
<h2>Sécurité</h2>
<ul>
<li>Toutes les connexions sont chiffrées en TLS. Après l'appairage, le switch n'accepte que le certificat de son TL-Manager, et TL-Manager vérifie l'empreinte de la clé du switch à chaque connexion.</li>
<li>Une commande poussée qui touche l'accès de gestion est confirmée par une nouvelle connexion. Si le switch n'est plus joignable, il revient seul à sa configuration précédente.</li>
<li>Interface web HTTPS, comptes locaux (admin ou lecture), mot de passe provisoire à changer, journal de toutes les actions.</li>
</ul>
</div>""".format(grille=grille, cartes="".join(cartes), lxc=e(lxc), gh=gh)
    return G.gabarit("TL-Manager · TL-SwitchOS", "TL-Manager : console centrale des switchs TL-SwitchOS, en ISO ou en "
                     "conteneur LXC.", corps, version, github, "tl-manager.html")

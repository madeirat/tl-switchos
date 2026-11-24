#!/usr/bin/env python3
"""TL-SwitchOS - site vitrine, genere par la recette de construction.

    python3 outils/site/generer-site.py <depot-git> <sortie> <depot-url> [proprietaire/depot-github]
            [--telechargements <dossier-local>]

<depot-url> : adresse publique du depot (ex. https://deb.exemple.fr/tlswitchos).
Les ISO sont lues dans <dossier-local> (par defaut <sortie>/telechargements) et
les liens pointent vers <depot-url>/telechargements/ : le site et les fichiers
lourds peuvent ainsi etre servis par deux machines differentes.

Ecrit dans <www> : index.html, fonctions.html, installation.html, commandes.html
(generee depuis la grammaire de la CLI), mises-a-jour.html, notes-de-version.html,
site.css, site.js, img/. Aucune ressource externe. Le sitemap.xml est ajoute par le
serveur web (il connait le domaine du site).
"""
import html
import os
import re
import shutil
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))


def md_inline(t):
    t = html.escape(t, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def changelog(chemin):
    """CHANGELOG.md -> [(version, html)]"""
    versions, cour, lignes = [], None, []
    try:
        texte = open(chemin, encoding="utf-8").read()
    except OSError:
        return []

    def fermer():
        if cour is None:
            return
        out, liste = [], False
        for l in lignes:
            if l.startswith("- "):
                if not liste:
                    out.append("<ul>")
                    liste = True
                out.append("<li>%s</li>" % md_inline(l[2:]))
            else:
                if liste:
                    out.append("</ul>")
                    liste = False
                if l.strip():
                    out.append("<p>%s</p>" % md_inline(l))
        if liste:
            out.append("</ul>")
        versions.append((cour, "\n".join(out)))

    for l in texte.splitlines():
        m = re.match(r"^## +(\S+)", l)
        if m:
            fermer()
            cour, lignes = m.group(1), []
        elif cour is not None:
            lignes.append(l.rstrip())
    fermer()
    return versions


def taille(n):
    for u in ("o", "Ko", "Mo", "Go"):
        if n < 1024 or u == "Go":
            return ("%d %s" % (n, u)) if u == "o" else ("%.1f %s" % (n, u)).replace(".", ",")
        n /= 1024.0


def cle_version(v):
    return [int(x) if x.isdigit() else 0 for x in v.split(".")]


def telechargements(dossier):
    """ISO presentes, de la plus recente a la plus ancienne."""
    isos = []
    if os.path.isdir(dossier):
        for f in os.listdir(dossier):
            m = re.match(r"^TL-SwitchOS-([0-9.]+)-amd64\.iso$", f)
            if not m:
                continue
            p = os.path.join(dossier, f)
            sha = ""
            try:
                sha = open(p + ".sha256").read().split()[0]
            except (OSError, IndexError):
                pass
            isos.append({"version": m.group(1), "fichier": f, "taille": os.path.getsize(p),
                         "sha": sha, "date": time.strftime("%d/%m/%Y", time.localtime(os.path.getmtime(p)))})
    isos.sort(key=lambda i: cle_version(i["version"]), reverse=True)
    return isos


FONCTIONS = [
    ("cli", "CLI d'administrateur réseau", "Les commandes que l'on connaît, avec abréviations, aide et complétion."),
    ("web", "Interface web", "Les ports en direct et toute la configuration, depuis le navigateur."),
    ("vlan", "VLAN, LACP, RSTP", "Access et trunk, agrégats de liens et protection contre les boucles."),
    ("supervision", "Supervision", "SNMPv3, syslog distant, export Prometheus et LLDP pour garder un œil sur le réseau."),
    ("maj", "Mises à jour signées", "Le système et Debian se mettent à jour ensemble, avec retour arrière automatique."),
    ("secours", "Sécurité des ports", "Port security, DHCP snooping, storm control, connexion RADIUS et sauvegarde automatique."),
]

ICONES = {
    "cli": '<path d="M4 6l5 5-5 5M11 17h8"/>',
    "web": '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M3 8h18M7 21h10"/>',
    "vlan": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>'
            '<rect x="8.5" y="14" width="7" height="7" rx="1.5"/><path d="M6.5 10v2h11v-2M12 12v2"/>',
    "supervision": '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    "maj": '<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "secours": '<path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.6"/><path d="M4 4v4.6h4.6"/>',
}


def icone(nom):
    return ('<svg class="ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">%s</svg>' % ICONES[nom])


GITHUB_ICO = ('<svg class="gh" viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8'
              'c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94'
              '-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64'
              '-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2'
              ' .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73'
              '.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')


PAGES = [  # (fichier, titre du menu)
    ("fonctions.html", "Fonctions"),
    ("installation.html", "Installation"),
    ("commandes.html", "Commandes"),
    ("mises-a-jour.html", "Mises à jour"),
    ("notes-de-version.html", "Versions"),
]


def gabarit(titre, description, corps, version, github, actuelle=""):
    """Squelette commun : en-tete avec menu, contenu, pied de page."""
    e = html.escape
    gh = "https://github.com/%s" % github if github else ""
    nav = "".join('<a href="%s"%s>%s</a>' % (f, ' aria-current="page"' if f == actuelle else "", e(l))
                  for f, l in PAGES)
    if gh:
        nav += '<a class="nav-gh" href="%s">%sGitHub</a>' % (e(gh), GITHUB_ICO)
    pied_gh = ('<a href="%s">%sCode source</a>' % (e(gh), GITHUB_ICO)) if gh else ""
    return """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>{titre}</title>
<meta name="description" content="{desc}">
<link rel="icon" href="img/favicon.png">
<link rel="stylesheet" href="site.css">
<script src="site.js" defer></script>
</head>
<body>
<header class="barre">
  <div class="largeur barre-in">
    <a class="marque" href="./"><img src="img/logo.svg" alt="" width="26" height="30">TL-SwitchOS</a>
    <nav aria-label="Pages">{nav}</nav>
  </div>
</header>

<main id="haut">
{corps}
</main>

<footer class="pied">
  <div class="largeur pied-in"><span>TL-SwitchOS {version} · TECH-LINUX</span>
  <span class="pied-liens"><a href="notes-de-version.html">Notes de version</a>{pied_gh}</span></div>
</footer>
</body>
</html>
""".format(titre=e(titre), desc=e(description), nav=nav, corps=corps, version=e(version), pied_gh=pied_gh)


def entete_doc(titre, chapo):
    return ('<section class="doc-tete"><div class="largeur"><h1>%s</h1><p class="chapo">%s</p></div></section>'
            % (titre, chapo))


def page(version, isos, versions, depot_url, captures, genere, github=""):
    e = html.escape
    dl = depot_url.rstrip("/") + "/telechargements/"
    gh = "https://github.com/%s" % github if github else ""
    der = isos[0] if isos else None
    gh_btn = '<a class="btn" href="%s">%sGitHub</a>' % (e(gh), GITHUB_ICO) if gh else ""
    if der:
        hero_btn = '<a class="btn primaire" href="#telecharger">Télécharger</a>'
        carte_dl = (
            '<div class="dl-carte"><div><p class="dl-titre">TL-SwitchOS {v}</p>'
            '<p class="muet">Image ISO · amd64 · {t}</p></div>'
            '<a class="btn primaire" href="{dl}{f}" download>Télécharger l\'ISO</a>'
            '<div class="sha"><span>SHA-256</span><code>{s}</code>'
            '<button class="copier" type="button" data-copie="{s}" aria-label="Copier le SHA-256">Copier</button></div>'
            '</div>').format(v=e(der["version"]), t=taille(der["taille"]), dl=e(dl), f=e(der["fichier"]),
                             s=e(der["sha"] or "?"))
    else:
        hero_btn = '<span class="btn desactive">ISO bientôt disponible</span>'
        carte_dl = ('<div class="dl-carte"><div><p class="dl-titre">TL-SwitchOS {v}</p>'
                    '<p class="muet">L\'image ISO arrive bientôt.</p></div></div>').format(v=e(version))
    plus = ('<p class="muet centre">Quoi de neuf : <a href="notes-de-version.html">notes de version</a>%s</p>'
            % (' · versions précédentes : <a href="%s/releases">GitHub</a>' % e(gh) if gh else ""))

    fonctions = "\n".join(
        '<article class="carte">%s<h3>%s</h3><p>%s</p></article>' % (icone(i), t, d) for i, t, d in FONCTIONS)
    apercu = ""
    if captures:
        f, l = captures[0]
        apercu = ('<section class="bloc largeur apercu"><figure><img src="img/%s" alt="%s" loading="lazy">'
                  '<figcaption>%s</figcaption></figure></section>' % (f, e(l), e(l)))

    corps = """<section class="hero">
  <div class="largeur hero-in">
    <p class="surtitre"><span class="point"></span>Version {version} disponible</p>
    <h1>Le switch réseau,<br><span class="degrade">sur un simple PC.</span></h1>
    <p class="chapo">TL-SwitchOS transforme un PC à plusieurs ports Ethernet en switch administrable de niveau 2, avec une CLI familière et une interface web.</p>
    <div class="actions">{hero_btn}{gh_btn}</div>
    <div class="console" aria-label="Exemple de session CLI">
<pre><span class="p">switch#</span> conf t
<span class="p">switch(config)#</span> interface Gi1/0/2
<span class="p">switch(config-if)#</span> switchport access vlan 10
<span class="p">switch(config-if)#</span> end
<span class="p">switch#</span> show vlan brief

VLAN Name        Status    Ports
---- ----------- --------- ----------------
1    default     active    Gi1/0/1
10   EXEMPLE     active    Gi1/0/2
<span class="p">switch#</span> wr
[OK]</pre>
    </div>
  </div>
</section>

<div class="puces largeur" aria-label="Protocoles">
  <span>VLAN 802.1Q</span><span>LACP</span><span>RSTP</span><span>LLDP</span><span>IGMP</span><span>SPAN</span><span>SNMPv3</span><span>RADIUS</span><span>Prometheus</span>
</div>

<section id="fonctions" class="bloc largeur">
  <h2>Tout ce qu'on attend d'un switch</h2>
  <p class="intro">Basé sur Debian, pensé pour être administré comme un vrai équipement réseau.</p>
  <div class="grille">
{fonctions}
  </div>
  <p class="suite"><a href="fonctions.html">Toutes les fonctions →</a></p>
</section>

{apercu}

<section id="installer" class="bloc largeur">
  <h2>Installé en quelques minutes</h2>
  <ol class="etapes">
    <li><span>1</span><h3>Graver</h3><p>L'ISO sur une clé USB.</p></li>
    <li><span>2</span><h3>Démarrer</h3><p>Sur la clé, puis répondre à deux questions.</p></li>
    <li><span>3</span><h3>Administrer</h3><p>En CLI ou depuis le navigateur.</p></li>
  </ol>
  <p class="muet centre">PC x86_64 · 2 ports Ethernet minimum · 2 Go de RAM · 10 Go de disque</p>
  <p class="suite"><a href="installation.html">Guide d'installation →</a></p>
</section>

<section id="telecharger" class="bloc largeur">
  <h2>Télécharger</h2>
  {carte_dl}
  {plus}
</section>""".format(version=e(version), hero_btn=hero_btn, gh_btn=gh_btn, fonctions=fonctions, apercu=apercu,
                       carte_dl=carte_dl, plus=plus)
    return gabarit("TL-SwitchOS", "TL-SwitchOS : un PC à plusieurs ports Ethernet devient un switch administrable.",
                   corps, version, github)


# --- pages de documentation -------------------------------------------------------

GROUPES = [
    ("Commutation", [
        ("VLAN 802.1Q", "Ports access et trunk, VLAN native, listes de VLAN autorisées, jusqu'à 4094 VLAN."),
        ("Agrégats LACP", "Port-channel en LACP actif ou passif, ou statique, répartition de charge niveau 3/4."),
        ("RSTP / STP", "Spanning-tree rapide, priorité du pont, PortFast et BPDU Guard sur les ports d'extrémité."),
        ("Multicast", "IGMP snooping actif par défaut, querier intégré pour les réseaux sans routeur multicast."),
        ("MTU jumbo", "Jusqu'à 9216 octets par port, pour le stockage et la virtualisation."),
        ("Port mirroring (SPAN)", "Copie du trafic d'un ou plusieurs ports vers une sonde ou un poste Wireshark."),
    ]),
    ("Sécurité des ports", [
        ("Port security", "Adresses MAC autorisées, nombre maximum par port, oubli automatique, action protect, restrict ou shutdown."),
        ("Err-disable", "Un port en violation est coupé, puis réactivé à la main ou automatiquement après un délai."),
        ("DHCP snooping", "Les serveurs DHCP pirates sont bloqués : seules les réponses des ports de confiance passent."),
        ("Storm control", "Limite de broadcast et de multicast par port, en paquets par seconde."),
        ("BPDU Guard", "Un port d'extrémité qui reçoit des BPDU est bloqué : pas de boucle par un switch branché par erreur."),
    ]),
    ("Supervision", [
        ("SNMP v2c et v3", "Lecture seule, SNMPv3 avec authentification SHA-2 et chiffrement AES."),
        ("Syslog distant", "Jusqu'à 4 serveurs en UDP ou TCP, niveau réglable, file d'attente si le serveur ne répond pas."),
        ("Export Prometheus", "Trafic, erreurs, rejets, état STP et MAC apprises par port, lisible aussi par Zabbix."),
        ("LLDP", "Voisins découverts et annoncés, visibles en CLI et sur le web."),
        ("NTP", "Heure synchronisée pour des journaux fiables."),
    ]),
    ("Administration", [
        ("CLI familière", "conf t, interface Gi1/0/3, show vlan brief, wr… avec abréviations, ? et Tab."),
        ("Interface web HTTPS", "Façade des ports en direct, configuration complète, console web, commandes CLI affichées."),
        ("Connexion RADIUS", "Administrateurs de l'annuaire (NPS, FreeRADIUS) en SSH et sur le web, comptes locaux en secours."),
        ("Comptes et niveaux", "Niveau 15 (administration) ou 1 (lecture seule), mot de passe provisoire à changer."),
        ("Sauvegarde automatique", "Copie horodatée de la configuration en SCP vers un NAS, à chaque enregistrement ou périodiquement."),
    ]),
    ("Fiabilité", [
        ("Retour arrière automatique", "Une modification qui coupe l'accès de gestion est annulée si elle n'est pas confirmée."),
        ("Démarrage sûr", "Configuration enregistrée, sinon sa sauvegarde, sinon configuration d'usine accessible."),
        ("Surveillance", "Vérification chaque minute : pont, services et ports remis en état si besoin."),
        ("Mises à jour signées", "Dépôt APT signé, test avant activation et retour à la version précédente en cas d'échec."),
    ]),
]


def page_fonctions(version, github):
    e = html.escape
    blocs = []
    for titre, items in GROUPES:
        blocs.append('<section class="doc-groupe"><h2>%s</h2><div class="grille">%s</div></section>' % (
            e(titre), "".join('<article class="carte"><h3>%s</h3><p>%s</p></article>' % (e(a), e(b))
                              for a, b in items)))
    corps = entete_doc("Fonctions", "Un switch de niveau 2 complet, administré comme un équipement réseau.") + \
        '<div class="largeur doc">%s</div>' % "".join(blocs)
    return gabarit("Fonctions · TL-SwitchOS", "Fonctions de TL-SwitchOS : VLAN, LACP, RSTP, port security, "
                   "DHCP snooping, SNMPv3, RADIUS, export Prometheus.", corps, version, github, "fonctions.html")


def page_installation(version, github):
    corps = entete_doc("Installation", "De la clé USB au switch administrable, en quelques minutes.") + """
<div class="largeur doc texte">
<h2>Matériel</h2>
<ul>
<li>PC x86_64 (BIOS ou UEFI) avec au moins 2 ports Ethernet, 2 Go de RAM et 10 Go de disque.</li>
<li>En machine virtuelle : une carte réseau virtuelle par port du switch.</li>
<li>Une connexion Internet sur un des ports pendant l'installation (paquets Debian).</li>
</ul>

<h2>Vérifier l'image</h2>
<p>Télécharge l'ISO et son fichier <code>.sha256</code>, puis :</p>
<pre>sha256sum -c TL-SwitchOS-{v}-amd64.iso.sha256</pre>
<p>La ligne doit se terminer par <code>OK</code>. Sous Windows : <code>Get-FileHash</code> dans PowerShell.</p>

<h2>Installer</h2>
<ol>
<li>Grave l'ISO sur une clé USB (Rufus en mode DD, balenaEtcher ou <code>dd</code>).</li>
<li>Démarre sur la clé. Le menu propose l'installation standard, l'installation par console série (115200 bauds) ou une installation Debian manuelle.</li>
<li>L'installateur, en français, pose deux questions : le <strong>nom du switch</strong> et la <strong>VLAN de gestion</strong>.</li>
<li>Étape réseau : DHCP automatique, ou IP fixe si tu la saisis. Le switch garde cette adresse sur son interface de gestion.</li>
<li>Un disque unique est effacé et partitionné sans question. S'il y en a plusieurs, l'installateur demande lequel utiliser.</li>
<li>TL-SwitchOS s'installe, puis la machine redémarre.</li>
</ol>

<h2>Premier démarrage</h2>
<p>L'écran de connexion affiche l'adresse du switch, mise à jour en direct :</p>
<pre>TL-SwitchOS {v} - switch01 - console tty1

  Vlan1     web : https://192.0.2.30   ssh : ssh admin@192.0.2.30

  Identifiant provisoire :  admin
  Mot de passe provisoire : admin</pre>
<p>À la première connexion, le switch exige un nouveau mot de passe (8 caractères minimum). Les ports sont nommés <code>Gi1/0/1</code> à <code>Gi1/0/N</code>, tous en access sur la VLAN de gestion.</p>

<h2>Accès</h2>
<table class="tableau">
<tr><th>Accès</th><th>Comment</th></tr>
<tr><td>Console</td><td>Écran local ou série <code>ttyS0</code> 115200 8N1, CLI directe</td></tr>
<tr><td>SSH</td><td><code>ssh admin@&lt;ip&gt;</code>, CLI directe</td></tr>
<tr><td>Web</td><td><code>https://&lt;ip&gt;</code> (certificat auto-signé)</td></tr>
<tr><td>Secours</td><td><code>start-shell</code> depuis la CLI niveau 15</td></tr>
</table>

<h2>Première configuration</h2>
<pre>conf t
hostname SWITCH01
vlan 10
 name UTILISATEURS
vlan 99
 name GESTION
interface Gi1/0/1
 description Uplink
 switchport mode trunk
 switchport trunk allowed vlan 10,99
interface range Gi1/0/2-8
 switchport access vlan 10
 spanning-tree portfast
interface Vlan99
 ip address 192.0.2.20 255.255.255.0
ip default-gateway 192.0.2.1
end
wr</pre>
<p>Toutes les commandes : <a href="commandes.html">référence des commandes</a>.</p>
</div>""".format(v=html.escape(version))
    return gabarit("Installation · TL-SwitchOS", "Installer TL-SwitchOS depuis une clé USB : matériel, étapes, "
                   "premier démarrage et première configuration.", corps, version, github, "installation.html")


MODES_DOC = [
    ("EXEC", "Mode exec", "Consultation, diagnostic, enregistrement (invite <code>switch#</code>)."),
    ("CONF", "Configuration globale", "Après <code>configure terminal</code> (invite <code>switch(config)#</code>)."),
    ("CONF_IF", "Interface", "Après <code>interface Gi1/0/3</code>, <code>interface range Gi1/0/1-8</code> ou <code>interface Po1</code>."),
    ("CONF_SVI", "Interface Vlan", "Après <code>interface Vlan99</code> : adresse IP de gestion."),
    ("CONF_VLAN", "VLAN", "Après <code>vlan 10</code>."),
]


def charger_commandes(depot):
    """Lit la grammaire de la CLI directement dans tlswitch.py : la page suit toujours le code."""
    import importlib.util
    sys.dont_write_bytecode = True          # pas de __pycache__ dans le depot
    chemin = os.path.join(depot, "tlswitch", "tlswitch.py")
    try:
        spec = importlib.util.spec_from_file_location("tlswitch_doc", chemin)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as ex:          # la page des commandes ne doit jamais bloquer la construction
        print("commandes : grammaire illisible (%s)" % ex, file=sys.stderr)
        return []
    res = []
    for nom, titre, intro in MODES_DOC:
        g = getattr(mod, nom, None)
        if g is None:
            continue
        vus, lignes = set(), []
        for c in g.cmds:
            motif = " ".join(tok for _, tok in c.tokens)
            if not c.help or motif in vus or motif in ("exit", "end"):
                continue
            vus.add(motif)
            lignes.append((motif, c.help))
        lignes.sort(key=lambda l: (l[0][3:] if l[0].startswith("no ") else l[0], l[0].startswith("no ")))
        res.append((titre, intro, lignes))
    return res


def page_commandes(version, github, depot):
    e = html.escape
    sections = []
    for titre, intro, lignes in charger_commandes(depot):
        rangs = "".join('<tr><td><code>%s</code></td><td>%s</td></tr>' % (
            e(m).replace("&lt;...&gt;", "&lt;…&gt;"), e(h)) for m, h in lignes)
        sections.append('<section class="doc-groupe"><h2>%s</h2><p class="muet">%s</p>'
                        '<table class="tableau commandes"><tr><th>Commande</th><th>Rôle</th></tr>%s</table></section>'
                        % (e(titre), intro, rangs))
    corps = entete_doc("Commandes", "Référence complète de la CLI, générée à partir du code de la version %s."
                       % e(version)) + """
<div class="largeur doc">
<div class="filtre"><input type="search" id="filtre" placeholder="Chercher une commande (ex. vlan, port-security, show)" aria-label="Chercher une commande"></div>
<p class="muet">Abréviations acceptées (<code>conf t</code>, <code>sh vl br</code>), <code>?</code> pour l'aide, Tab pour compléter, <code>do</code> pour lancer une commande exec depuis la configuration. Les mêmes commandes passent par l'interface web.</p>
{sections}
</div>""".format(sections="".join(sections))
    return gabarit("Commandes · TL-SwitchOS", "Référence des commandes de la CLI de TL-SwitchOS.",
                   corps, version, github, "commandes.html")


def page_maj(version, github):
    corps = entete_doc("Mises à jour", "Une nouvelle version s'installe sans réinstaller ni perdre la configuration.") + """
<div class="largeur doc texte">
<h2>Par le dépôt APT (recommandé)</h2>
<p>TL-SwitchOS a son propre dépôt APT signé. Une seule opération met à jour TL-SwitchOS et Debian, correctifs de sécurité compris.</p>
<ul>
<li><strong>CLI</strong> : <code>show install updates</code> liste ce qui est disponible, <code>install update</code> installe et propose de redémarrer.</li>
<li><strong>Web</strong> : menu Mise à jour, Rechercher les mises à jour, puis Installer. L'avancement s'affiche en direct.</li>
</ul>

<h2>Par fichier .tlsu</h2>
<p>Sans Internet, ou pour une mise à jour majeure (nouvelle version de Debian) :</p>
<ul>
<li><strong>Web</strong> : menu Mise à jour, choisir le fichier, Vérifier le paquet, puis Installer et redémarrer.</li>
<li><strong>CLI</strong> : <code>install add file usbflash0:TL-SwitchOS-{v}.tlsu activate</code>, ou une adresse <code>http://</code>.</li>
</ul>

<h2>Filet de sécurité</h2>
<ul>
<li>Paquets signés : un switch n'accepte que les mises à jour signées par la clé de l'ISO qui l'a installé.</li>
<li>Avant d'activer une version, le switch teste le nouveau code avec sa configuration et sauvegarde la version en place.</li>
<li>Si la nouvelle version ne démarre pas, le switch revient tout seul à la précédente. <code>install rollback</code> y revient à la demande.</li>
<li><code>show install summary</code> affiche la version installée, la précédente et l'historique.</li>
</ul>
<p>Le détail de chaque version : <a href="notes-de-version.html">notes de version</a>.</p>
</div>""".format(v=html.escape(version))
    return gabarit("Mises à jour · TL-SwitchOS", "Mettre à jour TL-SwitchOS : dépôt APT signé, fichier .tlsu, "
                   "retour automatique à la version précédente.", corps, version, github, "mises-a-jour.html")


def page_versions(version, github, versions):
    e = html.escape
    blocs = "".join('<section class="version" id="v%s"><h2>%s%s</h2>%s</section>' % (
        e(v), e(v), ' <span class="badge">actuelle</span>' if v == version else "", contenu)
        for v, contenu in versions)
    corps = entete_doc("Notes de version", "Ce qui change à chaque version de TL-SwitchOS.") + \
        '<div class="largeur doc texte">%s</div>' % (blocs or "<p>Aucune note de version.</p>")
    return gabarit("Notes de version · TL-SwitchOS", "Historique des versions de TL-SwitchOS.",
                   corps, version, github, "notes-de-version.html")


def ecrire(www, nom, contenu):
    tmp = os.path.join(www, "." + nom + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(contenu)
    os.replace(tmp, os.path.join(www, nom))


def main():
    args = [a for a in sys.argv[1:]]
    tele = None
    if "--telechargements" in args:
        i = args.index("--telechargements")
        tele = args[i + 1] if i + 1 < len(args) else sys.exit(__doc__)
        del args[i:i + 2]
    if len(args) not in (3, 4):
        sys.exit(__doc__)
    depot, www, depot_url = args[:3]
    depot_url = depot_url.rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.:-]+(/[A-Za-z0-9._/-]*)?$", depot_url):
        sys.exit("adresse du depot invalide : %s" % depot_url)
    github = args[3] if len(args) == 4 else ""
    if github and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", github):
        github = ""
    tele = tele or os.path.join(www, "telechargements")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', open(os.path.join(depot, "tlswitch", "tlswitch.py")).read(), re.M)
    version = m.group(1) if m else "?"

    img = os.path.join(www, "img")
    os.makedirs(img, exist_ok=True)  # cree aussi <sortie>
    for f in ("logo.svg", "favicon.png"):
        src = os.path.join(depot, "tlswitch", "web", f)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(img, f))
    captures = []
    for f, l in (("menu-demarrage.png", "Le menu de démarrage de l'ISO"),):
        src = os.path.join(depot, "docs", "captures", f)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(img, f))
            captures.append((f, l))
    for f in ("site.css", "site.js"):
        shutil.copyfile(os.path.join(ICI, f), os.path.join(www, f))

    versions = changelog(os.path.join(depot, "CHANGELOG.md"))
    ecrire(www, "index.html", page(version, telechargements(tele), versions, depot_url, captures,
                                   time.strftime("%d/%m/%Y à %H:%M"), github))
    ecrire(www, "fonctions.html", page_fonctions(version, github))
    ecrire(www, "installation.html", page_installation(version, github))
    ecrire(www, "commandes.html", page_commandes(version, github, depot))
    ecrire(www, "mises-a-jour.html", page_maj(version, github))
    ecrire(www, "notes-de-version.html", page_versions(version, github, versions))
    print("site genere : %s (%d pages)" % (www, 1 + len(PAGES)))


if __name__ == "__main__":
    main()

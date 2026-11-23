#!/usr/bin/env python3
"""TL-SwitchOS - site vitrine, genere par la recette de construction.

    python3 outils/site/generer-site.py <depot-git> <sortie> <depot-url> [proprietaire/depot-github]
            [--telechargements <dossier-local>]

<depot-url> : adresse publique du depot (ex. https://deb.exemple.fr/tlswitchos).
Les ISO sont lues dans <dossier-local> (par defaut <sortie>/telechargements) et
les liens pointent vers <depot-url>/telechargements/ : le site et les fichiers
lourds peuvent ainsi etre servis par deux machines differentes.

Ecrit dans <www> (dossier servi par nginx, a cote du depot APT) :
  index.html, site.css, site.js, img/ (logo, capture du menu de demarrage)
Page autonome : aucune ressource externe.
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
    ("supervision", "Supervision", "LLDP, SNMP, NTP et journaux pour garder un œil sur le réseau."),
    ("maj", "Mises à jour signées", "Le système et Debian se mettent à jour ensemble, avec retour arrière automatique."),
    ("secours", "Filet de sécurité", "Une erreur coupe l'accès ? Le switch revient seul à sa configuration précédente."),
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
    plus = ('<p class="muet centre">Versions précédentes et notes de version : '
            '<a href="%s/releases">GitHub</a></p>' % e(gh)) if gh else ""

    fonctions = "\n".join(
        '<article class="carte">%s<h3>%s</h3><p>%s</p></article>' % (icone(i), t, d) for i, t, d in FONCTIONS)
    apercu = ""
    if captures:
        f, l = captures[0]
        apercu = ('<section class="bloc largeur apercu"><figure><img src="img/%s" alt="%s" loading="lazy">'
                  '<figcaption>%s</figcaption></figure></section>' % (f, e(l), e(l)))

    return """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>TL-SwitchOS</title>
<meta name="description" content="TL-SwitchOS : un PC à plusieurs ports Ethernet devient un switch administrable.">
<link rel="icon" href="img/favicon.png">
<link rel="stylesheet" href="site.css">
<script src="site.js" defer></script>
</head>
<body>
<header class="barre">
  <div class="largeur barre-in">
    <a class="marque" href="#haut"><img src="img/logo.svg" alt="" width="26" height="30">TL-SwitchOS</a>
    <nav aria-label="Sections">
      <a href="#fonctions">Fonctions</a><a href="#installer">Installer</a><a href="#telecharger">Télécharger</a>{nav_gh}
    </nav>
  </div>
</header>

<main id="haut">
<section class="hero">
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
  <span>VLAN 802.1Q</span><span>LACP</span><span>RSTP</span><span>LLDP</span><span>SNMP</span><span>SSH</span><span>HTTPS</span>
</div>

<section id="fonctions" class="bloc largeur">
  <h2>Tout ce qu'on attend d'un switch</h2>
  <p class="intro">Basé sur Debian, pensé pour être administré comme un vrai équipement réseau.</p>
  <div class="grille">
{fonctions}
  </div>
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
</section>

<section id="telecharger" class="bloc largeur">
  <h2>Télécharger</h2>
  {carte_dl}
  {plus}
</section>
</main>

<footer class="pied">
  <div class="largeur pied-in"><span>TL-SwitchOS {version} · TECH-LINUX</span>{pied_gh}</div>
</footer>
</body>
</html>
""".format(version=e(version), hero_btn=hero_btn, gh_btn=gh_btn, fonctions=fonctions, apercu=apercu,
           carte_dl=carte_dl, plus=plus,
           nav_gh=('<a class="nav-gh" href="%s">%sGitHub</a>' % (e(gh), GITHUB_ICO)) if gh else "",
           pied_gh=('<a href="%s">%sCode source</a>' % (e(gh), GITHUB_ICO)) if gh else "")


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

    contenu = page(version, telechargements(tele),
                   changelog(os.path.join(depot, "CHANGELOG.md")), depot_url, captures,
                   time.strftime("%d/%m/%Y à %H:%M"), github)
    tmp = os.path.join(www, ".index.html.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(contenu)
    os.replace(tmp, os.path.join(www, "index.html"))
    print("site genere : %s/index.html" % www)


if __name__ == "__main__":
    main()

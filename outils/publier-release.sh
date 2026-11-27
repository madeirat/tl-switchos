#!/bin/bash
# =============================================================================
#  TL-SwitchOS - recette de construction, lancee par le serveur de construction
#  ("construction lancer tlswitchos", voir outils/lxc/README.md).
#
#  Le serveur fournit (variables d'environnement) :
#    DEPOT      clone git du projet (deja a jour sur la branche)
#    CLES       cles du projet (creees ici au premier lancement)
#    CACHE      fichiers gardes entre deux constructions (netinst Debian)
#    WWW        dossier prepare, publie ensuite dans /srv/depot/<projet> : depot APT, ISO, site signe
#    DEPOT_URL  adresse publique de ce dossier (switchs et visiteurs du site)
#    GH_REPO, GH_TOKEN   pour la Release GitHub
#    PUBLIER_PAGES=oui   copie aussi le depot APT sur GitHub Pages (branche gh-pages)
#
#  Si la release vX.Y.Z (VERSION de tlswitch/tlswitch.py) n'existe pas encore :
#    .tlsu signe + .deb -> depot APT signe (WWW) -> ISO -> site vitrine signe (WWW) -> Release GitHub.
#  Le serveur publie ensuite WWW (nginx) ; le serveur web y recupere le site.
#  Mise a jour majeure (Debian) : MAJEURE_DEBIAN=forky -> .tlsu seul, pas de .deb.
# =============================================================================
set -euo pipefail
: "${DEPOT:?}" "${CLES:?}" "${CACHE:?}" "${WWW:?}" "${DEPOT_URL:?}" "${GH_REPO:?}"
export GH_REPO GH_TOKEN="${GH_TOKEN:-}"
log() { echo "$(date '+%F %T')  $*"; }
mkdir -p "$CACHE" "$WWW"; install -d -m 700 "$CLES"

cd "$DEPOT"
VERSION=$(python3 tlswitch/tlswitch.py version)
TAG="v$VERSION"
# site vitrine : archive signee (cle du depot), recuperee et verifiee par le serveur web.
# Archive reproductible : meme contenu = meme empreinte, le serveur web ne republie que si ca change.
# telechargements : seulement la derniere ISO (les anciennes versions restent dans le depot APT
# et les Releases). L'ancienne ISO n'est retiree que 30 min apres l'arrivee de la nouvelle :
# le serveur web reprend le site toutes les 10 min, son lien ne doit jamais tomber sur une 404.
# Meme regle pour l'image compacte et le live CD (ajoutes par l'etape root de la release).
purger_iso() {
  local T="$WWW/telechargements" N s
  for s in amd64.iso amd64.img.xz amd64-live.iso; do
    N=$(ls "$T"/TL-SwitchOS-[0-9]*-$s 2>/dev/null | sort -V | tail -n 1)
    [ -n "$N" ] && [ -n "$(find "$N" -mmin +30)" ] || continue
    ls "$T"/TL-SwitchOS-[0-9]*-$s | sort -V | head -n -1 | while read -r v; do rm -f "$v" "$v.sha256"; log "fichier retire : ${v##*/}"; done
  done
}
site() {
  local S="$CACHE/site" A="$WWW/site/site.tar.gz" FP
  # script d'installation sur une Debian deja installee : adresse du depot + empreinte de sa cle
  FP=$(gpg --show-keys --with-colons "$CLES/depot-apt.gpg" 2>/dev/null | sed -n 's/^fpr:*\([0-9A-F]*\):*$/\1/p' | head -n 1)
  if [ -n "$FP" ]; then
    sed -e "s#__DEPOT_URL__#$DEPOT_URL#g" -e "s#__EMPREINTE__#$FP#g" outils/installer-sur-debian.sh > "$WWW/installer.sh.tmp"
    chmod 644 "$WWW/installer.sh.tmp"; mv "$WWW/installer.sh.tmp" "$WWW/installer.sh"
  fi
  rm -rf "$S"
  python3 outils/site/generer-site.py "$DEPOT" "$S" "$DEPOT_URL" "$GH_REPO" --telechargements "$WWW/telechargements"
  mkdir -p "$WWW/site"
  tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' -cf - -C "$S" . | gzip -9n > "$A.tmp"
  if [ -f "$A" ] && [ -f "$A.sig" ] && cmp -s "$A.tmp" "$A"; then rm -f "$A.tmp"; return 0; fi
  mv "$A.tmp" "$A"
  bash outils/depot-apt.sh signer "$CLES" "$A"
  log "site vitrine signe : $DEPOT_URL/site/site.tar.gz"
}

if gh release view "$TAG" >/dev/null 2>&1; then
  # version deja publiee : seul le site suit le code (pages, notes de version)
  purger_iso
  [ -s "$CLES/depot-apt.gpg" ] && site
  log "$TAG deja publiee, rien d'autre a faire"; exit 0
fi
log "nouvelle version $VERSION : construction"

# cles : lien vers le dossier du serveur (jamais dans le depot git)
rm -rf cles && ln -s "$CLES" cles
trap 'rm -f "$DEPOT/cles" "$DEPOT/tlswitch/depot-apt.conf" "$DEPOT/tlswitch/depot-apt.gpg"' EXIT

# notes de version = section "## X.Y.Z" du CHANGELOG
NOTES=$(sed -n "/^## $VERSION\$/,/^## /{/^## /d;p}" CHANGELOG.md 2>/dev/null | sed '/./,$!d')
[ -n "$NOTES" ] || NOTES="TL-SwitchOS $VERSION"

# depot APT : adresse + cle publique embarquees dans le .tlsu et dans l'ISO
bash outils/depot-apt.sh cle "$CLES" >/dev/null
printf 'DEPOT_URL=%s\n' "$DEPOT_URL" > tlswitch/depot-apt.conf
cp "$CLES/depot-apt.gpg" tlswitch/depot-apt.gpg

# paquet de mise a jour signe (cree cles/maj-privee.pem au premier lancement) + paquet Debian
TYPE_ARGS=()
if [ -n "${MAJEURE_DEBIAN:-}" ]; then TYPE_ARGS=(--majeure "$MAJEURE_DEBIAN"); fi
bash fabriquer-maj.sh "${TYPE_ARGS[@]}" "$NOTES"
TLSU="TL-SwitchOS-$VERSION.tlsu"
[ -f "$TLSU" ] || { echo "ERREUR : paquet .tlsu non produit"; exit 1; }
DEB=""
if [ -z "${MAJEURE_DEBIAN:-}" ]; then
  bash fabriquer-deb.sh "$TLSU"
  DEB="tlswitch_${VERSION}_all.deb"
fi

# ISO (netinst Debian prise dans le cache, telechargee une seule fois)
NETINST=$(ls -t "$CACHE"/debian-*-amd64-netinst.iso 2>/dev/null | head -n 1 || true)
if [ -n "$NETINST" ]; then
  DEBIAN_ISO="$NETINST" bash construire-iso.sh
else
  TLS_APPELANT="$CACHE" bash construire-iso.sh
  ls ../debian-*-amd64-netinst.iso >/dev/null 2>&1 && mv ../debian-*-amd64-netinst.iso "$CACHE"/ || true
fi
ISO="TL-SwitchOS-$VERSION-amd64.iso"
[ -f "$ISO" ] || { echo "ERREUR : ISO non produite"; exit 1; }

# .sha256 avec le seul nom de fichier (pour sha256sum -c apres telechargement)
FICHIERS=("$ISO" "$TLSU" ${DEB:+"$DEB"})
for f in "${FICHIERS[@]}"; do sha256sum "$f" | sed 's#  .*/#  #' > "$f.sha256"; done

# depot APT servi par nginx
if [ -n "$DEB" ]; then
  bash outils/depot-apt.sh ajouter "$CLES" "$WWW" "$DEB"
  log "depot APT a jour : $DEPOT_URL"
fi

# telechargements : la nouvelle ISO ; l'ancienne est retiree plus tard par purger_iso
T="$WWW/telechargements"; mkdir -p "$T"
cp -f "$ISO" "$ISO.sha256" "$T/"

site

# copie optionnelle sur GitHub Pages (depot APT + site, sans les ISO)
if [ -n "$DEB" ] && [ "${PUBLIER_PAGES:-non}" = oui ]; then
  P="$CACHE/gh-pages"
  if [ ! -d "$P/.git" ]; then
    git clone -q "$(git remote get-url origin)" "$P"
    git -C "$P" fetch -q origin gh-pages 2>/dev/null \
      && git -C "$P" checkout -q -B gh-pages origin/gh-pages \
      || { git -C "$P" checkout -q --orphan gh-pages; git -C "$P" rm -rqf . 2>/dev/null || true; }
  else
    git -C "$P" fetch -q origin gh-pages && git -C "$P" reset -q --hard origin/gh-pages || true
  fi
  rsync -a --delete --exclude .git --exclude telechargements --exclude site "$WWW/" "$P/"
  git -C "$P" add -A
  git -C "$P" -c user.name="construction" -c user.email="construction@localhost" commit -qm "depot APT : tlswitch $VERSION"
  git -C "$P" push -q origin gh-pages
  log "depot APT copie sur GitHub Pages"
fi

gh release create "$TAG" "${FICHIERS[@]}" "${FICHIERS[@]/%/.sha256}" \
  --target "$(git rev-parse HEAD)" --title "TL-SwitchOS $VERSION" --notes "$NOTES"
log "$TAG publiee : ${FICHIERS[*]}"

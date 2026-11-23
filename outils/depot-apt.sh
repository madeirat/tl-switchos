#!/bin/bash
# =============================================================================
#  TL-SwitchOS - depot APT signe
#
#     bash outils/depot-apt.sh cle  <dossier-cles>
#         cree (une seule fois) la cle GPG du depot dans <dossier-cles>/gnupg
#         et exporte la cle publique : <dossier-cles>/depot-apt.gpg
#     bash outils/depot-apt.sh ajouter <dossier-cles> <dossier-depot> paquet.deb...
#         copie les .deb dans le depot, regenere les index et les signe
#     bash outils/depot-apt.sh signer <dossier-cles> fichier
#         signature detachee fichier.sig (ex. archive du site, verifiee par gpgv
#         sur le serveur web avec tlswitch-depot.gpg)
#
#  Structure produite (a servir en HTTP(S)) :
#     <depot>/pool/main/t/tlswitch/tlswitch_X.Y.Z_all.deb
#     <depot>/dists/stable/{Release,InRelease,Release.gpg}
#     <depot>/dists/stable/main/binary-amd64/Packages{,.gz}  (paquets Architecture: all)
#     <depot>/tlswitch-depot.gpg        cle publique du depot
#
#  Sur le switch : deb [signed-by=/usr/share/keyrings/tlswitch-depot.gpg] <url> stable main
#  Necessite : gpg, apt-utils (apt-ftparchive), dpkg-dev.
# =============================================================================
set -euo pipefail
ACTION="${1:?usage : depot-apt.sh cle|ajouter ...}"; shift
CLES="${1:?dossier des cles}"; shift
export GNUPGHOME="$CLES/gnupg"     # chemin court (< 80 caracteres) : gpg-agent y cree sa socket
install -d -m 700 "$GNUPGHOME"
ID="TL-SwitchOS depot APT"

cle() {
  if ! gpg --batch --list-secret-keys "$ID" >/dev/null 2>&1; then
    gpg --batch --pinentry-mode loopback --passphrase '' \
        --quick-generate-key "$ID" ed25519 sign never >/dev/null 2>&1
    echo "==> Nouvelle cle GPG du depot dans $GNUPGHOME (A SAUVEGARDER hors ligne, avec maj-privee.pem)"
  fi
  gpg --batch --export "$ID" > "$CLES/depot-apt.gpg"
}

case "$ACTION" in
  cle) cle; echo "cle publique : $CLES/depot-apt.gpg" ;;
  signer)
    F="${1:?fichier a signer}"; cle
    gpg --batch --yes --local-user "$ID" --detach-sign -o "$F.sig" "$F"
    ;;
  ajouter)
    DEPOT="${1:?dossier du depot}"; shift
    cle
    install -d "$DEPOT/pool/main/t/tlswitch"
    for d in "$@"; do cp "$d" "$DEPOT/pool/main/t/tlswitch/"; done
    cd "$DEPOT"
    install -d dists/stable/main/binary-amd64
    apt-ftparchive packages pool > dists/stable/main/binary-amd64/Packages
    gzip -9nkf dists/stable/main/binary-amd64/Packages
    apt-ftparchive \
      -o APT::FTPArchive::Release::Origin=TL-SwitchOS \
      -o APT::FTPArchive::Release::Label=TL-SwitchOS \
      -o APT::FTPArchive::Release::Suite=stable \
      -o APT::FTPArchive::Release::Codename=stable \
      -o APT::FTPArchive::Release::Architectures=amd64 \
      -o APT::FTPArchive::Release::Components=main \
      release dists/stable > dists/stable/Release.tmp
    mv dists/stable/Release.tmp dists/stable/Release
    rm -f dists/stable/InRelease dists/stable/Release.gpg
    gpg --batch --yes --local-user "$ID" --clearsign -o dists/stable/InRelease dists/stable/Release
    gpg --batch --yes --local-user "$ID" -abs -o dists/stable/Release.gpg dists/stable/Release
    cp "$CLES/depot-apt.gpg" tlswitch-depot.gpg
    touch .nojekyll                      # GitHub Pages : servir les fichiers tels quels
    echo "Depot a jour : $(ls pool/main/t/tlswitch | tr '\n' ' ')"
    ;;
  *) echo "action inconnue : $ACTION"; exit 1 ;;
esac

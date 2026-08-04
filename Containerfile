# Native container build for the Kinoite + ZFS image.
#
# This repository intentionally avoids BlueBuild. The build is expressed as a
# standard bootc-style Containerfile so CI can control tags directly.

# Fedora Kinoite is the upstream Fedora KDE Atomic desktop image. It does not
# carry the optional Homebrew payload, so import that payload as a
# separate stage below while keeping the operating-system base Fedora-owned.
ARG BASE_IMAGE="quay.io/fedora-ostree-desktops/kinoite:44"
ARG BREW_IMAGE="ghcr.io/ublue-os/brew:latest"

FROM scratch AS ctx
COPY build_files /
COPY containerfiles /containerfiles
COPY files /files
COPY shared /shared
COPY cosign.pub /cosign.pub

FROM ${BREW_IMAGE} AS brew

FROM ${BASE_IMAGE}

# These build arguments are supplied by CI for each run.
#
# Local builds should not bake in one Fedora major version here. When CI does
# not pass an explicit akmods image reference, the helper can render this
# template with the Fedora version detected from the chosen base image.
#
# ARG values declared in this stage are already visible as shell environment
# variables to the RUN instruction below, so build-image.sh can read them
# directly without a separate ENV block.
ARG AKMODS_IMAGE=""
ARG AKMODS_IMAGE_TEMPLATE="ghcr.io/danathar/zfs-kinoite-complex-akmods:main-{fedora}"
ARG IMAGE_REPO="ghcr.io/danathar/zfs-kinoite-complex"
ARG SIGNING_KEY_FILENAME="zfs-kinoite-complex.pub"

# Fedora Kinoite does not include the optional Homebrew payload.
COPY --from=brew /system_files /

# Bind-mount the build context instead of COPYing it so none of these files
# (build-image.sh, containerfiles/, files/, shared/, cosign.pub) end up baked
# into the published image's filesystem.
RUN --mount=type=bind,from=ctx,source=/,target=/ctx \
    --mount=type=cache,target=/var/cache \
    --mount=type=cache,target=/var/log \
    --mount=type=tmpfs,target=/tmp \
    /ctx/build-image.sh

# Fedora Kinoite sets `quay.expires-after=4w` on its own images. That label is inert on
# GitHub Container Registry (GHCR), but it rides along into this image, and any
# mirror of this image to a Quay-backed registry would start self-deleting after
# four weeks. This image is meant to be a durable update target, so clear it.
LABEL quay.expires-after=

RUN bootc container lint

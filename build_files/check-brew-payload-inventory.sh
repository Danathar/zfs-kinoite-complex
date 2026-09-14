#!/usr/bin/env bash
#
# Script: build_files/check-brew-payload-inventory.sh
# What: Compares the ublue-os/brew payload's complete file list against build_files/brew-payload.manifest.
# Doing: Walks the bind-mounted payload, sorts every non-directory entry it finds, and fails the build on any difference from the committed list.
# Why: `COPY --from=brew /system_files /` lands a third-party tree in this image's root; the digest pin in ci/defaults.json makes that payload reproducible but not reviewed.
# Goal: Stop the build on a path nobody read, before that path is in the image and before anything acts on it.
#
# Deliberately a separate script rather than another function in build-image.sh, and
# deliberately invoked from its own RUN *above* `COPY --from=brew /system_files /`.
# build-image.sh runs after that COPY, so by the time it starts, the payload owns the
# filesystem the check would have to run on: a payload shipping usr/bin/find,
# usr/bin/grep, usr/bin/sed or bin/sh replaces the very tools that would report it and
# can make its own additions pass. Running here, the shell and every utility below come
# from the Fedora base image and nothing from the payload has been copied yet.
#
set -euo pipefail

# Paths only, not hashes. The tarball's bytes change on every upstream release, and a
# check that fires every time is a check that gets skipped. A new *path* is new
# surface -- a unit drop-in, a tmpfiles.d entry, an /etc/profile replacement -- and
# that is what should stop a build until someone has read it.
check_brew_payload_inventory() {
  local payload="${1:-/brew-payload}"
  local manifest="${2:-/ctx/brew-payload.manifest}"
  local landed expected
  if [ ! -d "${payload}" ]; then
    echo "Brew payload not mounted for inspection: ${payload}" >&2
    echo "The Containerfile RUN step must bind-mount the brew stage there." >&2
    return 1
  fi
  # `! -type d` rather than a list of the types worth worrying about. Regular files and
  # symlinks are what the payload ships today, but `COPY --from=brew` will carry a FIFO,
  # a socket or a character/block device just as happily, and an entry this walk does not
  # produce is an entry the comparison cannot miss. Every non-directory node counts.
  landed="$(cd "${payload}" && find . ! -type d -printf '%P\n' | LC_ALL=C sort)"
  expected="$(sed -e 's/#.*//' -e 's/[[:space:]]*$//' "${manifest}" | grep -v '^$' | LC_ALL=C sort)"
  if [ "${landed}" != "${expected}" ]; then
    # Reported with grep rather than diff: diffutils is not guaranteed to be in the
    # base image, and a check that cannot explain itself is most of a check wasted.
    echo "The brew payload no longer matches build_files/brew-payload.manifest:" >&2
    printf '%s\n' "${landed}" | grep -vxF -f <(printf '%s\n' "${expected}") \
      | sed 's/^/  added:   /' >&2 || true
    printf '%s\n' "${expected}" | grep -vxF -f <(printf '%s\n' "${landed}") \
      | sed 's/^/  missing: /' >&2 || true
    echo "Read each added file before listing it there. It is copied into / and signed." >&2
    return 1
  fi
}

check_brew_payload_inventory "$@"

#!/usr/bin/env bash
# Point the meta-calculinux bitbake recipe at VERSION / REV / LICENSE_MD5.
set -euo pipefail

self_check() {
  # Not local: EXIT trap must still see $tmp after the function returns.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  cat >"$tmp/calculinux-update_0.3.2.bb" <<'EOF'
SRC_URI = "git://github.com/Calculinux/calculinux-update.git;branch=main;protocol=https"
SRCREV = "oldrevoldrevoldrevoldrevoldrevoldrevoldrev"
LIC_FILES_CHKSUM = "file://LICENSE;md5=oldmd5oldmd5oldmd5oldmd5oldmd5old"
EOF
  VERSION=0.7.0 REV=0123456789abcdef0123456789abcdef01234567 \
    LICENSE_MD5=1ebbd3e34237af26da5dc08a4e440464 \
    RECIPE_DIR="$tmp" bash "$0" --apply
  test -f "$tmp/calculinux-update_0.7.0.bb"
  test ! -f "$tmp/calculinux-update_0.3.2.bb"
  grep -q 'SRCREV = "0123456789abcdef0123456789abcdef01234567"' \
    "$tmp/calculinux-update_0.7.0.bb"
  grep -q 'md5=1ebbd3e34237af26da5dc08a4e440464' \
    "$tmp/calculinux-update_0.7.0.bb"
  rm "$tmp/calculinux-update_0.7.0.bb"
  cat >"$tmp/calculinux-update_git.bb" <<'EOF'
SRCREV = "oldrevoldrevoldrevoldrevoldrevoldrevoldrev"
LIC_FILES_CHKSUM = "file://LICENSE;md5=oldmd5oldmd5oldmd5oldmd5oldmd5old"
PV = "0.6.0+git${SRCPV}"
EOF
  VERSION=0.7.2 REV=0123456789abcdef0123456789abcdef01234567 \
    LICENSE_MD5=1ebbd3e34237af26da5dc08a4e440464 \
    RECIPE_DIR="$tmp" bash "$0" --apply
  test -f "$tmp/calculinux-update_git.bb"
  grep -q 'PV = "0.7.2+git${SRCPV}"' "$tmp/calculinux-update_git.bb"
  grep -q 'SRCREV = "0123456789abcdef0123456789abcdef01234567"' \
    "$tmp/calculinux-update_git.bb"
  echo ok
}

apply() {
  : "${VERSION:?}" "${REV:?}" "${LICENSE_MD5:?}"
  local dir="${RECIPE_DIR:-meta-calculinux-distro/recipes-core/calculinux-update}"
  local old new tmp
  old=$(ls "$dir"/calculinux-update_*.bb)
  if [ "$(echo "$old" | wc -l)" -ne 1 ]; then
    echo "expected exactly one recipe in $dir, found:" >&2
    echo "$old" >&2
    exit 1
  fi
  # A _git.bb recipe carries the version in PV; a versioned one in its name.
  case "$old" in
    *_git.bb) new="$old" ;;
    *) new="$dir/calculinux-update_${VERSION}.bb" ;;
  esac
  if [ "$old" != "$new" ]; then
    if [ "${USE_GIT_MV:-}" = 1 ]; then
      git mv "$old" "$new"
    else
      mv "$old" "$new"
    fi
  fi
  tmp=$(mktemp)
  sed \
    -e "s/^SRCREV = \".*\"/SRCREV = \"${REV}\"/" \
    -e "s/^PV = \"[^+\"]*+git/PV = \"${VERSION}+git/" \
    -e "s|LIC_FILES_CHKSUM = \"file://LICENSE;md5=[^\"]*\"|LIC_FILES_CHKSUM = \"file://LICENSE;md5=${LICENSE_MD5}\"|" \
    "$new" >"$tmp"
  mv "$tmp" "$new"
}

case "${1:-}" in
  --self-check) self_check ;;
  --apply) apply ;;
  *)
    echo "usage: $0 --self-check | --apply" >&2
    exit 2
    ;;
esac

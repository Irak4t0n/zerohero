#!/bin/bash
# Build the 0Hero .deb (run on the CardputerZero or any Debian box with python3-pil).
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(python3 -c "import json;print(json.load(open('app-builder.json'))['version'])")
PKG=zerohero
ROOT=build/root
rm -rf build && mkdir -p "$ROOT/DEBIAN" \
  "$ROOT/usr/share/APPLaunch/apps/$PKG" "$ROOT/usr/share/APPLaunch/applications" \
  "$ROOT/usr/share/APPLaunch/share/images" "$ROOT/usr/bin" "$ROOT/usr/share/doc/$PKG" assets/screenshots

install -m 0755 src/app.py "$ROOT/usr/share/APPLaunch/apps/$PKG/app.py"
install -m 0755 src/zerohero_core.py "$ROOT/usr/share/APPLaunch/apps/$PKG/zerohero_core.py"
install -m 0644 packaging/zerohero.desktop "$ROOT/usr/share/APPLaunch/applications/$PKG.desktop"
install -m 0755 packaging/zerohero "$ROOT/usr/bin/zerohero"
install -m 0644 packaging/copyright "$ROOT/usr/share/doc/$PKG/copyright"

python3 tools/make_icon.py assets/icon.png 256 >/dev/null
install -m 0644 assets/icon.png "$ROOT/usr/share/APPLaunch/share/images/$PKG.png"
for s in list running done; do
  python3 src/app.py --preview "assets/screenshots/$s.png" --screen "$s"
done

cat > "$ROOT/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Architecture: all
Maintainer: Irak4t0n <https://github.com/Irak4t0n>
Depends: python3, python3-pil, fonts-dejavu-core, sudo
Section: utils
Priority: optional
Homepage: https://github.com/Irak4t0n/zerohero
Description: Update every installed CardputerZero store app in one tap
 0Hero compares the apps you installed from the CardputerZero App Store with
 the official registry and installs every available update through the
 Store's own verified transaction flow. Newly installed apps are picked up
 automatically. Includes the zerohero command for SSH use.
EOF
find "$ROOT" -name '*.pyc' -delete
dpkg-deb --root-owner-group --build "$ROOT" "build/${PKG}_${VERSION}_all.deb"
echo "built build/${PKG}_${VERSION}_all.deb"

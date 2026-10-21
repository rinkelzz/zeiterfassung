#!/bin/bash
# Baut zeiterfassung-<version>-noarch.spk für DSM 7 – ein Paket für Intel/AMD (x86_64)
# und ARM (aarch64 = ARMv8, armv7l = ARMv7). Reines Python liegt je Python-Version unter
# lib/<cp>, kompilierte Bibliotheken je Prozessor unter lib/<cp>-<prozessor>.
# Benötigt: Python 3 mit pip, Internetzugang zu PyPI.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
VERSION="$(python3 -c "import re;print(re.search(r'\"(.+)\"', open('$ROOT/app/__init__.py').read()).group(1))")"
BUILD_NO="${BUILD_NO:-0001}"
OUT="${OUT:-$ROOT/dist}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Version $VERSION-$BUILD_NO"
PKG="$WORK/package"
mkdir -p "$PKG/lib" "$OUT"

ARCHES="aarch64 armv7l"          # zusätzlich zu x86_64
BINARY_PKGS="pydantic_core"      # die einzigen kompilierten Abhängigkeiten

echo "==> Bibliotheken je Python-Version"
for TAG in cp38 cp39 cp310 cp311 cp312 cp313; do
    PYVER="${TAG:2:1}.${TAG:3}"
    W="$WORK/wheels/$TAG"
    pip download -q --only-binary=:all: --platform manylinux2014_x86_64 --implementation cp \
        --python-version "$PYVER" --abi "$TAG" -d "$W" -r "$ROOT/requirements.txt"
    # Marker-Abhängigkeiten, die pip beim Cross-Download nicht immer auflöst
    EXTRA=""
    [ "$TAG" = cp38 ] && EXTRA="backports.zoneinfo==0.2.1"
    case "$TAG" in cp38|cp39|cp310) EXTRA="$EXTRA exceptiongroup>=1.0.2" ;; esac
    if [ -n "$EXTRA" ]; then
        pip download -q --no-deps --only-binary=:all: --platform manylinux1_x86_64 \
            --platform manylinux2014_x86_64 --implementation cp --python-version "$PYVER" \
            --abi "$TAG" -d "$W" $EXTRA
    fi
    mkdir -p "$PKG/lib/$TAG"
    for whl in "$W"/*.whl; do
        python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$whl" "$PKG/lib/$TAG"
    done
    # backports.zoneinfo (nur cp38) bringt eine reine Python-Fassung mit – die läuft auf jedem Prozessor
    find "$PKG/lib/$TAG" -name "_czoneinfo*.so" -delete
    # kompilierte Pakete je Prozessor ablegen
    mkdir -p "$PKG/lib/$TAG-x86_64"
    for BP in $BINARY_PKGS; do
        DI=$(ls -d "$PKG/lib/$TAG/$BP"-*.dist-info)
        BV=$(basename "$DI" .dist-info); BV=${BV#"$BP"-}
        mv "$PKG/lib/$TAG/$BP" "$DI" "$PKG/lib/$TAG-x86_64/"
        for A in $ARCHES; do
            WA="$WORK/wheels/$TAG-$A"
            pip download -q --no-deps --only-binary=:all: --platform "manylinux2014_$A" --implementation cp \
                --python-version "$PYVER" --abi "$TAG" -d "$WA" "$BP==$BV"
            mkdir -p "$PKG/lib/$TAG-$A"
            for whl in "$WA"/*.whl; do
                python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$whl" "$PKG/lib/$TAG-$A"
            done
        done
    done
    # Schutz: im gemeinsamen Ordner darf nichts Kompiliertes mehr liegen
    LEFT=$(find "$PKG/lib/$TAG" -name "*.so" | head -3)
    if [ -n "$LEFT" ]; then echo "FEHLER: kompilierte Dateien in lib/$TAG – BINARY_PKGS ergänzen:"; echo "$LEFT"; exit 1; fi
    echo "    $TAG: $(ls "$W" | wc -l) Pakete, $BINARY_PKGS für x86_64 $ARCHES"
done
find "$PKG/lib" -name "__pycache__" -type d -prune -exec rm -rf {} +

echo "==> Anwendung"
cp -r "$ROOT/app" "$PKG/app"
find "$PKG/app" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp -r "$HERE/ui" "$PKG/ui"
rm -f "$PKG/ui/app.json"
chmod 755 "$PKG/ui/app.cgi"
cp "$HERE/proxy/dsm_proxy.py" "$PKG/dsm_proxy.py"
cp "$HERE/zeiterfassung.sc" "$PKG/"

echo "==> package.tgz"
SPK="$WORK/spk"
mkdir -p "$SPK/scripts"
tar --owner=0 --group=0 -czf "$SPK/package.tgz" -C "$PKG" .

echo "==> Skripte"
for s in preinst postinst start-stop-status; do
    { echo "#!/bin/sh"; cat "$HERE/scripts/common"; cat "$HERE/scripts/$s"; } > "$SPK/scripts/$s"
done
for s in preuninst postuninst preupgrade postupgrade; do
    printf '#!/bin/sh\nexit 0\n' > "$SPK/scripts/$s"
done
chmod 755 "$SPK/scripts/"*

cp -r "$HERE/conf" "$HERE/WIZARD_UIFILES" "$SPK/"
cp "$HERE/PACKAGE_ICON.PNG" "$HERE/PACKAGE_ICON_256.PNG" "$SPK/"
sed "s/@VERSION@/$VERSION-$BUILD_NO/" "$HERE/INFO.in" > "$SPK/INFO"
echo "checksum=\"$(md5sum "$SPK/package.tgz" | cut -d' ' -f1)\"" >> "$SPK/INFO"

NAME="zeiterfassung-$VERSION-$BUILD_NO-noarch.spk"
( cd "$SPK" && tar --owner=0 --group=0 -cf "$OUT/$NAME" INFO package.tgz scripts conf WIZARD_UIFILES PACKAGE_ICON.PNG PACKAGE_ICON_256.PNG )
echo "==> Fertig: $OUT/$NAME ($(du -h "$OUT/$NAME" | cut -f1))"

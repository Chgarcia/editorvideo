#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Este script debe ejecutarse en macOS." >&2
  exit 1
fi

PYTHON_EXE="${PYTHON_EXE:-python3}"
FFMPEG_EXE="$(command -v ffmpeg || true)"
FFPROBE_EXE="$(command -v ffprobe || true)"

if [[ -z "$FFMPEG_EXE" || -z "$FFPROBE_EXE" ]]; then
  echo "Instala FFmpeg antes de compilar (por ejemplo: brew install ffmpeg)." >&2
  exit 1
fi

"$PYTHON_EXE" -m pip install --upgrade pyinstaller

"$PYTHON_EXE" -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name editvideo \
  --osx-bundle-identifier mx.gob.oaj.editvideo \
  --distpath dist \
  --workpath build/macos/work \
  --specpath build/macos/spec \
  --add-data "$PWD/Manual_de_usuario.html:." \
  --add-binary "$FFMPEG_EXE:." \
  --add-binary "$FFPROBE_EXE:." \
  muteador.py

ditto -c -k --sequesterRsrc --keepParent dist/editvideo.app dist/editvideomac.zip
"$PYTHON_EXE" updater.py manifest macos dist/editvideomac.zip

echo
echo "Aplicación generada en: $PWD/dist/editvideo.app"
echo "Paquete ZIP generado en: $PWD/dist/editvideomac.zip"
echo "Manifiesto de versión en: $PWD/dist/version.json"
echo "Configura EDITVIDEO_UPDATE_BASE_URL con la URL HTTPS donde se publicaran las actualizaciones."

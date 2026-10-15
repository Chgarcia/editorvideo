"""Version check and self-update support for editvideo.

The server folder must contain the binaries plus a ``version.json`` manifest
generated at build time with ``python updater.py manifest <platform> <file>``.
"""

import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request

APP_VERSION = "1.0.3"
APP_NAME = "editvideo"
UPDATE_BASE_URL = os.environ.get("EDITVIDEO_UPDATE_BASE_URL", "").rstrip("/")
MANIFEST_URL = f"{UPDATE_BASE_URL}/version.json" if UPDATE_BASE_URL else ""
PLATFORM_FILES = {
    "windows": "editvideo.exe",
    "macos": "editvideomac.zip",
}
REQUEST_TIMEOUT = 10
CHUNK_SIZE = 256 * 1024


class UpdateError(Exception):
    pass


def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def parse_version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"\s*v?(\d+(?:\.\d+){0,3})\s*", str(text))
    if not match:
        raise UpdateError(f"Versión inválida: {text!r}")
    parts = [int(p) for p in match.group(1).split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def is_newer(remote: str, local: str = APP_VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def _open(url: str, timeout: float):
    if not url.lower().startswith("https://"):
        raise UpdateError("Solo se permiten actualizaciones por HTTPS.")
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context())


def fetch_manifest(url: str = MANIFEST_URL, timeout: float = REQUEST_TIMEOUT) -> dict:
    if not url:
        raise UpdateError("La URL de actualizaciones no está configurada (EDITVIDEO_UPDATE_BASE_URL).")
    try:
        with _open(url, timeout) as response:
            data = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except UpdateError:
        raise
    except Exception as ex:
        raise UpdateError(f"No se pudo consultar la versión disponible: {ex}") from ex
    if not isinstance(data, dict) or "version" not in data:
        raise UpdateError("El archivo de versión del servidor no es válido.")
    parse_version(data["version"])
    return data


def check_for_update(manifest_url: str = MANIFEST_URL, platform: str | None = None, local_version: str = APP_VERSION):
    """Return update info dict when a newer version exists for this platform, else None."""
    platform = platform or current_platform()
    manifest = fetch_manifest(manifest_url)
    if not is_newer(manifest["version"], local_version):
        return None
    entry = manifest.get(platform)
    if not isinstance(entry, dict):
        return None
    file_name = entry.get("file") or PLATFORM_FILES.get(platform)
    if not file_name:
        return None
    base_url = manifest_url.rsplit("/", 1)[0] + "/"
    return {
        "version": str(manifest["version"]),
        "notes": str(manifest.get("notes", "")).strip(),
        "url": entry.get("url") or base_url + file_name,
        "sha256": str(entry.get("sha256", "")).lower(),
        "size": entry.get("size"),
    }


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_update(info: dict, destination: str, progress=None, timeout: float = 60) -> str:
    """Download to ``destination`` verifying size and SHA-256. Removes partial files on failure."""
    expected_hash = info.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise UpdateError("La actualización no tiene una huella SHA-256 válida.")

    partial = destination + ".part"
    digest = hashlib.sha256()
    received = 0
    try:
        with _open(info["url"], timeout) as response, open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or info.get("size") or 0)
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress:
                    progress(received, total)
        if info.get("size") and received != int(info["size"]):
            raise UpdateError("La descarga está incompleta.")
        if digest.hexdigest() != expected_hash:
            raise UpdateError("La huella SHA-256 de la descarga no coincide.")
        os.replace(partial, destination)
        return destination
    except UpdateError:
        _silent_remove(partial)
        raise
    except Exception as ex:
        _silent_remove(partial)
        raise UpdateError(f"No se pudo descargar la actualización: {ex}") from ex


def _silent_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def can_self_update() -> bool:
    return current_platform() == "windows" and bool(getattr(sys, "frozen", False))


def installed_executable() -> str:
    return os.path.abspath(sys.executable)


def is_writable_dir(directory: str) -> bool:
    try:
        fd, probe = tempfile.mkstemp(prefix=".update_probe_", dir=directory)
        os.close(fd)
        os.remove(probe)
        return True
    except OSError:
        return False


def build_replace_script(new_file: str, target: str, log_file: str) -> str:
    # Retries the move until the running exe releases its file lock, then relaunches it.
    return (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        "set /a tries=0\r\n"
        ":retry\r\n"
        f'move /y "{new_file}" "{target}" >nul 2>&1\r\n'
        "if not errorlevel 1 goto launch\r\n"
        "set /a tries+=1\r\n"
        "if %tries% geq 120 goto fail\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto retry\r\n"
        ":fail\r\n"
        f'echo No se pudo reemplazar "{target}" >> "{log_file}"\r\n'
        f'del /f /q "{new_file}" >nul 2>&1\r\n'
        ":launch\r\n"
        "set PYINSTALLER_RESET_ENVIRONMENT=1\r\n"
        f'start "" "{target}"\r\n'
        '(goto) 2>nul & del /f /q "%~f0"\r\n'
    )


def stage_windows_update(info: dict, progress=None) -> str:
    """Download next to the installed exe and launch the replace script. Caller must exit right after."""
    target = installed_executable()
    directory = os.path.dirname(target)
    if not is_writable_dir(directory):
        raise UpdateError(
            f"No hay permisos para escribir en:\n{directory}\n\n"
            "Copia la aplicación a una carpeta de tu usuario o actualiza manualmente."
        )
    new_file = os.path.join(directory, f"{APP_NAME}.new.exe")
    download_update(info, new_file, progress)

    script = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_update_{os.getpid()}.cmd")
    log_file = os.path.join(tempfile.gettempdir(), f"{APP_NAME}_update.log")
    with open(script, "w", encoding="utf-8", newline="") as f:
        f.write(build_replace_script(new_file, target, log_file))

    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["cmd.exe", "/c", script],
        cwd=tempfile.gettempdir(),
        env=env,
        creationflags=flags,
        close_fds=True,
    )
    return new_file


def write_manifest(platform: str, file_path: str, manifest_path: str, version: str = APP_VERSION, notes: str = "") -> dict:
    """Create or merge ``version.json`` with the hash of a freshly built package."""
    manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            manifest = {}
    if manifest.get("version") != version:
        manifest = {"version": version}
    if notes:
        manifest["notes"] = notes
    manifest[platform] = {
        "file": os.path.basename(file_path),
        "sha256": sha256_file(file_path),
        "size": os.path.getsize(file_path),
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return manifest


def merge_manifests(output_path: str, *paths: str) -> dict:
    merged = {}
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if merged and data.get("version") != merged.get("version"):
            raise UpdateError(f"Versiones distintas: {merged.get('version')} y {data.get('version')}")
        merged.update(data)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return merged


def set_app_version(new_version: str, source_path: str = __file__) -> str:
    """Rewrite APP_VERSION in this module; the new version must be greater than the current one."""
    parse_version(new_version)
    normalized = new_version.strip().lstrip("v")
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    match = re.search(r'^APP_VERSION = "([^"]*)"$', source, flags=re.MULTILINE)
    if not match:
        raise UpdateError("No se encontró APP_VERSION en updater.py.")
    if not is_newer(normalized, match.group(1)):
        raise UpdateError(f"La versión {normalized} debe ser mayor que la actual ({match.group(1)}).")
    source = source[:match.start(1)] + normalized + source[match.end(1):]
    with open(source_path, "w", encoding="utf-8", newline="") as f:
        f.write(source)
    return normalized


def suggest_next_version(version: str = APP_VERSION) -> str:
    parts = list(parse_version(version)[:3])
    parts[2] += 1
    return ".".join(map(str, parts))


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "manifest":
        _, _, platform_name, package = sys.argv[:4]
        target_manifest = os.path.join(os.path.dirname(os.path.abspath(package)), "version.json")
        result = write_manifest(platform_name, package, target_manifest, notes=os.environ.get("EDITVIDEO_NOTES", ""))
        print(f"Manifiesto generado: {target_manifest} (versión {result['version']})")
    elif len(sys.argv) >= 4 and sys.argv[1] == "merge":
        result = merge_manifests(sys.argv[2], *sys.argv[3:])
        print(f"Manifiesto combinado: {sys.argv[2]} (versión {result['version']})")
    elif len(sys.argv) == 2 and sys.argv[1] == "version":
        print(APP_VERSION)
    elif len(sys.argv) == 2 and sys.argv[1] == "next-version":
        print(suggest_next_version())
    elif len(sys.argv) == 3 and sys.argv[1] == "set-version":
        try:
            print(f"Versión actualizada a {set_app_version(sys.argv[2])}")
        except UpdateError as ex:
            print(f"ERROR: {ex}", file=sys.stderr)
            sys.exit(1)
    else:
        print(
            "Uso: python updater.py manifest <windows|macos> <archivo>\n"
            "     python updater.py merge <salida.json> <a.json> <b.json>\n"
            "     python updater.py set-version <x.y.z>\n"
            "     python updater.py next-version\n"
            "     python updater.py version"
        )
        sys.exit(2)

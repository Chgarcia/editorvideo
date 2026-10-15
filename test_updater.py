import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import updater


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def fake_open(routes):
    def _open(url, timeout):
        if url not in routes:
            raise OSError(f"404 {url}")
        return FakeResponse(routes[url])
    return _open


class VersionTests(unittest.TestCase):
    def test_parse_and_compare_versions(self):
        self.assertEqual(updater.parse_version("1.2"), (1, 2, 0, 0))
        self.assertEqual(updater.parse_version("v2.0.1"), (2, 0, 1, 0))
        self.assertTrue(updater.is_newer("1.0.10", "1.0.9"))
        self.assertTrue(updater.is_newer("2", "1.9.9"))
        self.assertFalse(updater.is_newer("1.0.0", "1.0"))
        self.assertFalse(updater.is_newer("0.9", "1.0.0"))
        for bad in ["", "abc", "1..2", "1.2.3.4.5", "1.0-beta"]:
            with self.subTest(bad=bad), self.assertRaises(updater.UpdateError):
                updater.parse_version(bad)

    def test_only_https_is_allowed(self):
        with self.assertRaisesRegex(updater.UpdateError, "HTTPS"):
            updater._open("http://example.test/editvideo/version.json", 1)

    def test_update_url_is_configurable(self):
        self.assertEqual(
            updater.MANIFEST_URL,
            f"{updater.UPDATE_BASE_URL}/version.json" if updater.UPDATE_BASE_URL else "",
        )
        self.assertEqual(updater.PLATFORM_FILES["windows"], "editvideo.exe")
        self.assertEqual(updater.PLATFORM_FILES["macos"], "editvideomac.zip")

    def test_missing_update_url_raises_clear_error(self):
        with patch.object(updater, "MANIFEST_URL", ""):
            with self.assertRaisesRegex(updater.UpdateError, "EDITVIDEO_UPDATE_BASE_URL"):
                updater.fetch_manifest()


class CheckTests(unittest.TestCase):
    url = "https://example.test/editvideo/version.json"

    def _check(self, manifest, platform="windows", local="1.0.0"):
        routes = {self.url: json.dumps(manifest).encode()}
        with patch.object(updater, "_open", fake_open(routes)):
            return updater.check_for_update(self.url, platform, local)

    def test_newer_version_returns_download_info(self):
        info = self._check({
            "version": "1.1.0", "notes": "Mejoras",
            "windows": {"file": "editvideo.exe", "sha256": "A" * 64, "size": 10},
            "macos": {"file": "editvideomac.zip", "sha256": "b" * 64},
        })
        self.assertEqual(info["version"], "1.1.0")
        self.assertEqual(info["url"], "https://example.test/editvideo/editvideo.exe")
        self.assertEqual(info["sha256"], "a" * 64)
        self.assertEqual(info["notes"], "Mejoras")

        mac = self._check({"version": "1.1.0", "macos": {"sha256": "b" * 64}}, platform="macos")
        self.assertEqual(mac["url"], "https://example.test/editvideo/editvideomac.zip")

    def test_same_or_older_version_returns_none(self):
        entry = {"windows": {"sha256": "a" * 64}}
        self.assertIsNone(self._check({"version": "1.0.0", **entry}))
        self.assertIsNone(self._check({"version": "0.9.0", **entry}))

    def test_missing_platform_returns_none(self):
        self.assertIsNone(self._check({"version": "2.0.0", "macos": {"sha256": "a" * 64}}))

    def test_invalid_or_unreachable_manifest_raises(self):
        for body in [b"no json", b"[]", b'{"notes": "x"}', b'{"version": "x.y"}']:
            with self.subTest(body=body), patch.object(updater, "_open", fake_open({self.url: body})):
                with self.assertRaises(updater.UpdateError):
                    updater.check_for_update(self.url, "windows", "1.0.0")
        with patch.object(updater, "_open", fake_open({})), self.assertRaisesRegex(updater.UpdateError, "No se pudo"):
            updater.check_for_update(self.url, "windows", "1.0.0")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = os.path.join(self.tmp.name, "editvideo.new.exe")
        self.payload = os.urandom(600_000)
        self.url = "https://example.test/editvideo/editvideo.exe"

    def tearDown(self):
        self.tmp.cleanup()

    def _download(self, sha, size=None, progress=None):
        info = {"url": self.url, "sha256": sha, "size": size}
        with patch.object(updater, "_open", fake_open({self.url: self.payload})):
            return updater.download_update(info, self.dest, progress)

    def test_download_verifies_hash_and_reports_progress(self):
        calls = []
        self._download(hashlib.sha256(self.payload).hexdigest(), len(self.payload), lambda r, t: calls.append((r, t)))
        with open(self.dest, "rb") as f:
            self.assertEqual(f.read(), self.payload)
        self.assertEqual(calls[-1], (len(self.payload), len(self.payload)))
        self.assertFalse(os.path.exists(self.dest + ".part"))

    def test_hash_mismatch_or_bad_size_discards_file(self):
        for sha, size, message in [
            ("0" * 64, None, "SHA-256"),
            (hashlib.sha256(self.payload).hexdigest(), 5, "incompleta"),
            ("", None, "huella"),
        ]:
            with self.subTest(message=message), self.assertRaisesRegex(updater.UpdateError, message):
                self._download(sha, size)
            self.assertFalse(os.path.exists(self.dest))
            self.assertFalse(os.path.exists(self.dest + ".part"))


class ManifestTests(unittest.TestCase):
    def test_set_app_version_only_moves_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "updater.py")
            with open(source, "w", encoding="utf-8") as f:
                f.write('X = 1\nAPP_VERSION = "1.0.1"\nY = "APP_VERSION"\n')
            self.assertEqual(updater.set_app_version("v1.2.0", source), "1.2.0")
            with open(source, encoding="utf-8") as f:
                self.assertEqual(f.read(), 'X = 1\nAPP_VERSION = "1.2.0"\nY = "APP_VERSION"\n')
            for bad in ["1.2.0", "1.1.9", "abc"]:
                with self.subTest(bad=bad), self.assertRaises(updater.UpdateError):
                    updater.set_app_version(bad, source)
        self.assertEqual(updater.suggest_next_version("1.0.9"), "1.0.10")

    def test_write_and_merge_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "editvideo.exe")
            mac = os.path.join(tmp, "editvideomac.zip")
            for path, data in [(exe, b"win"), (mac, b"mac")]:
                with open(path, "wb") as f:
                    f.write(data)
            win_manifest = os.path.join(tmp, "win.json")
            mac_manifest = os.path.join(tmp, "mac.json")
            updater.write_manifest("windows", exe, win_manifest, version="1.2.0")
            updater.write_manifest("macos", mac, mac_manifest, version="1.2.0", notes="Nuevo")
            merged = updater.merge_manifests(os.path.join(tmp, "version.json"), win_manifest, mac_manifest)

            self.assertEqual(merged["version"], "1.2.0")
            self.assertEqual(merged["windows"], {
                "file": "editvideo.exe", "sha256": hashlib.sha256(b"win").hexdigest(), "size": 3,
            })
            self.assertEqual(merged["macos"]["file"], "editvideomac.zip")
            self.assertEqual(merged["notes"], "Nuevo")

            updater.write_manifest("windows", exe, mac_manifest, version="1.3.0")
            with self.assertRaisesRegex(updater.UpdateError, "Versiones distintas"):
                updater.merge_manifests(os.path.join(tmp, "x.json"), win_manifest, mac_manifest)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class ReplaceScriptTests(unittest.TestCase):
    def test_script_waits_for_lock_then_replaces_and_relaunches(self):
        with tempfile.TemporaryDirectory(prefix="edit vídeo ") as tmp:
            target = os.path.join(tmp, "app.cmd")
            new_file = os.path.join(tmp, "app.new.cmd")
            marker = os.path.join(tmp, "launched.txt")
            with open(target, "w") as f:
                f.write("@echo old\r\n")
            with open(new_file, "w", encoding="utf-8") as f:
                f.write('@echo nueva > "%~dp0launched.txt"\r\nexit\r\n')

            holder = subprocess.Popen([
                sys.executable, "-c",
                f"import time; f = open({target!r}, 'rb'); open({target!r} + '.held', 'w').close(); time.sleep(3)",
            ])
            deadline = time.monotonic() + 10
            while not os.path.exists(target + ".held"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)

            script = os.path.join(tmp, "update.cmd")
            with open(script, "w", encoding="utf-8", newline="") as f:
                f.write(updater.build_replace_script(new_file, target, os.path.join(tmp, "log.txt")))
            started = time.monotonic()
            subprocess.run(["cmd.exe", "/c", script], check=True, timeout=60)
            holder.wait(10)

            deadline = time.monotonic() + 10
            while not os.path.exists(marker):
                self.assertLess(time.monotonic(), deadline, "the new version was not launched")
                time.sleep(0.1)
            self.assertGreater(time.monotonic() - started, 1.0, "replace should wait for the lock")
            with open(target, encoding="utf-8") as f:
                self.assertIn("nueva", f.read())
            self.assertFalse(os.path.exists(new_file))
            self.assertFalse(os.path.exists(script))
            self.assertFalse(os.path.exists(os.path.join(tmp, "log.txt")))


if __name__ == "__main__":
    unittest.main()

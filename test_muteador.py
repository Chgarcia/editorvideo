import os
import shutil
import subprocess
import tempfile
import time
import unittest
from tkinter import messagebox, ttk
from unittest.mock import patch

import updater
from muteador import App, BITRATE_SAFETY_FACTOR, COMPRESSED_RENDER_HINT, is_valid_partial_time, is_valid_time, parse_time_to_seconds

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def run(*args):
    return subprocess.run(args, check=True, capture_output=True).stdout


class AppTests(unittest.TestCase):
    def setUp(self):
        with patch.object(App, "_load_saved_ranges"):
            self.app = App()
        self.app.withdraw()

    def tearDown(self):
        self.app.destroy()

    def _set_rows(self, rows):
        self.app.clear_rows()
        for index, (start, end, mute, blur) in enumerate(rows):
            if index >= len(self.app.rows):
                self.app.add_row()
            row = self.app.rows[index]
            row.start_var.set(start)
            row.end_var.set(end)
            row.mute_var.set(mute)
            row.blur_var.set(blur)

    def test_processing_locks_controls_and_restores_them(self):
        self._set_rows([("1", "2", True, False)])
        row = self.app.rows[0]
        row.blur_check.state(["disabled"])

        self.app._set_processing_state(True)
        self.assertTrue(self.app.is_processing)
        for widget in (self.app.process_button, row.start_entry, row.end_entry, row.mute_check, row.remove_button):
            self.assertTrue(widget.instate(["disabled"]), widget)
        self.assertEqual(str(self.app.cget("cursor")), "watch")
        self.assertEqual(str(row.start_entry.cget("cursor")), "watch")

        self.app._set_processing_state(False)
        self.assertFalse(self.app.is_processing)
        for widget in (self.app.process_button, row.start_entry, row.end_entry, row.mute_check, row.remove_button):
            self.assertFalse(widget.instate(["disabled"]), widget)
        self.assertTrue(row.blur_check.instate(["disabled"]))
        self.assertEqual(str(self.app.cget("cursor")), "")

    def test_manual_is_available_from_help_menu_and_main_window(self):
        menubar = self.app.nametowidget(self.app.cget("menu"))
        self.assertEqual(menubar.entrycget(0, "label"), "Ayuda")
        menu = self.app.nametowidget(menubar.entrycget(0, "menu"))
        self.assertEqual(menu.entrycget(0, "label"), "Manual de usuario")
        self.assertEqual(menu.entrycget(2, "label"), "Buscar actualización")
        pending = [self.app]
        buttons = []
        while pending:
            parent = pending.pop()
            for widget in parent.winfo_children():
                pending.append(widget)
                if isinstance(widget, ttk.Button):
                    buttons.append(widget.cget("text"))
        self.assertIn("Manual de usuario", buttons)

    def test_open_user_manual_uses_local_manual_resource(self):
        with patch("muteador.webbrowser.open", return_value=True) as open_browser:
            self.app.open_user_manual()
        uri = open_browser.call_args.args[0]
        self.assertTrue(uri.endswith("/Manual_de_usuario.html"))
        open_browser.assert_called_once_with(uri, new=2)

    def test_open_user_manual_reports_browser_errors(self):
        with patch("muteador.webbrowser.open", side_effect=OSError("sin navegador")), \
                patch.object(messagebox, "showerror") as showerror:
            self.app.open_user_manual()
        self.assertIn("sin navegador", showerror.call_args.args[1])

    def _pump_updates(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.app.update_in_progress:
            self.app.update()
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_manual_update_check_reports_latest_version(self):
        with patch("muteador.updater.check_for_update", return_value=None), \
                patch.object(messagebox, "showinfo") as showinfo:
            self.app.check_for_updates(manual=True)
            self._pump_updates()
        self.assertIn("versión más reciente", showinfo.call_args[0][1])

    def test_update_check_errors_are_silent_unless_manual(self):
        with patch("muteador.updater.check_for_update", side_effect=updater.UpdateError("sin red")), \
                patch.object(messagebox, "showerror") as showerror:
            self.app.check_for_updates(manual=False)
            self._pump_updates()
            showerror.assert_not_called()
            self.app.check_for_updates(manual=True)
            self._pump_updates()
        self.assertIn("sin red", showerror.call_args[0][1])

    def test_update_without_self_update_opens_download(self):
        info = {"version": "9.0.0", "notes": "", "url": "https://example.test/editvideo/editvideomac.zip"}
        with patch("muteador.updater.check_for_update", return_value=info), \
                patch("muteador.updater.can_self_update", return_value=False), \
                patch.object(messagebox, "askyesno", return_value=True), \
                patch("muteador.webbrowser.open") as browser:
            self.app.check_for_updates(manual=False)
            self._pump_updates()
        browser.assert_called_once_with(info["url"])

    def test_windows_update_downloads_then_closes_app(self):
        info = {"version": "9.0.0", "notes": "Mejoras", "url": "https://x/editvideo.exe", "sha256": "a" * 64}

        def fake_stage(_info, progress):
            progress(50, 100)
            progress(100, 100)

        with patch("muteador.updater.check_for_update", return_value=info), \
                patch("muteador.updater.can_self_update", return_value=True), \
                patch("muteador.updater.stage_windows_update", side_effect=fake_stage) as stage, \
                patch.object(messagebox, "askyesno", return_value=True) as ask, \
                patch.object(self.app, "destroy") as destroy:
            self.app.check_for_updates(manual=False)
            self._pump_updates()
        self.assertIn("Mejoras", ask.call_args[0][1])
        stage.assert_called_once()
        destroy.assert_called_once()
        self.assertEqual(self.app.download_var.get(), 100.0)

    def test_update_prompt_waits_until_processing_finishes(self):
        info = {"version": "9.0.0", "notes": "", "url": "https://x/editvideo.exe"}
        self.app._set_processing_state(True)
        with patch.object(messagebox, "askyesno", return_value=False) as ask, \
                patch.object(self.app, "after") as after:
            self.app._handle_update_result(False, info)
            ask.assert_not_called()
            after.assert_called_once()
        self.app._set_processing_state(False)

    def test_each_range_applies_only_its_selected_effects(self):
        self._set_rows([
            ("1", "2", True, False),
            ("3", "4", False, True),
            ("5", "6", True, True),
        ])
        ranges = self.app._get_ranges()

        self.assertEqual(
            self.app._build_audio_filter(ranges),
            "volume=0:enable='between(t,1.000,2.000)',"
            "volume=0:enable='between(t,5.000,6.000)'",
        )
        self.assertEqual(
            self.app._build_video_filter(ranges),
            "gblur=sigma=50:enable='between(t,3.000,4.000)',"
            "gblur=sigma=50:enable='between(t,5.000,6.000)'",
        )

    def test_range_without_effects_is_allowed_for_rendering(self):
        self._set_rows([("1", "2", False, False)])
        ranges = self.app._get_ranges()
        self.assertEqual(len(ranges), 1)
        self.assertFalse(ranges[0]["mute"])
        self.assertFalse(ranges[0]["blur"])
        self.assertEqual(self.app._build_audio_filter(ranges), "")
        self.assertEqual(self.app._build_video_filter(ranges), "")

    def test_valid_time_formats_are_parsed(self):
        cases = {
            "12": 12.0,
            "12.5": 12.5,
            "12,25": 12.25,
            "01:05": 65.0,
            "1:05.500": 65.5,
            "01:02:03.250": 3723.25,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertAlmostEqual(parse_time_to_seconds(text), expected)

    def test_invalid_time_formats_are_rejected(self):
        for text in ["abc", "1a", "-1", "1:2:3:4", "00:60", "01:75:00", "1.2.3", "1:", ":5", "1e3", "inf", "nan", "1.2345"]:
            with self.subTest(text=text):
                self.assertFalse(is_valid_time(text))
                with self.assertRaises(ValueError):
                    parse_time_to_seconds(text)

    def test_invalid_range_time_reports_row_number(self):
        self._set_rows([("1", "2", True, False), ("abc", "5", True, False)])
        with self.assertRaisesRegex(ValueError, "rango 2 tiene un inicio inválido"):
            self.app._get_ranges()

    def test_time_entries_block_non_time_characters(self):
        for text in ["", "1", "01:02", "01:02:03.5", "12,5"]:
            with self.subTest(text=text):
                self.assertTrue(is_valid_partial_time(text))
        for text in ["a", "1-2", "1 2", "1:2:3:4", "1.2.3", "1.5:3"]:
            with self.subTest(text=text):
                self.assertFalse(is_valid_partial_time(text))

    def test_overlapping_ranges_are_rejected(self):
        cases = [
            [("00:05", "00:10", True, False), ("00:08", "00:12", False, True)],
            [("0", "20", True, False), ("5", "6", True, False)],
            [("3", "4", True, False), ("3", "4", False, True)],
            [("10", "15", True, False), ("1", "2", True, False), ("14.999", "16", True, False)],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self._set_rows(rows)
                with self.assertRaisesRegex(ValueError, "se traslapan"):
                    self.app._get_ranges()

    def test_adjacent_ranges_are_allowed(self):
        self._set_rows([("5", "10", True, False), ("0", "5", False, True), ("10", "12", True, True)])
        ranges = self.app._get_ranges()
        self.assertEqual([r["index"] for r in ranges], [2, 1, 3])

    def test_ranges_must_fit_within_video_duration(self):
        self._set_rows([("1", "2", True, False), ("9", "10", True, False)])
        self.app._validate_ranges_within_duration(self.app._get_ranges(), 10.0)

        for rows, message in [
            ([("10", "11", True, False)], "rango 1 inicia en 00:10.000, fuera del video"),
            ([("8", "10.5", True, False)], "rango 1 termina en 00:10.500, después del final"),
            ([("1", "2", True, False), ("00:00:09", "00:01:00", True, False)], "rango 2 termina"),
        ]:
            with self.subTest(rows=rows):
                self._set_rows(rows)
                with self.assertRaisesRegex(ValueError, message):
                    self.app._validate_ranges_within_duration(self.app._get_ranges(), 10.0)

    def test_process_rejects_range_beyond_video_duration(self):
        if not HAS_FFMPEG:
            self.skipTest("FFmpeg no disponible")
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.mp4")
            self._make_source(source)
            self.app.video_path.set(source)
            self.app.output_path.set(os.path.join(tmp, "out.mp4"))
            self._set_rows([("1", "99", True, False)])
            with patch.object(messagebox, "showerror") as showerror:
                self.app.process_video()
            showerror.assert_called_once()
            self.assertIn("después del final del video", showerror.call_args[0][1])
            self.assertFalse(self.app.is_processing)
            self.assertFalse(os.path.exists(os.path.join(tmp, "out.mp4")))

    def test_codec_args_copy_untouched_stream(self):
        self.app.compress_render_var.set(False)
        mute_only = self.app._build_codec_args("volume=0", "")
        self.assertIn("-af", mute_only)
        self.assertNotIn("-vf", mute_only)
        self.assertEqual(mute_only[mute_only.index("-c:v") + 1], "copy")
        self.assertEqual(mute_only[mute_only.index("-c:a") + 1], "aac")

        blur_only = self.app._build_codec_args("", "gblur=sigma=50")
        self.assertEqual(blur_only[blur_only.index("-c:v") + 1], "libx264")
        self.assertEqual(blur_only[blur_only.index("-c:a") + 1], "copy")

        both = self.app._build_codec_args("volume=0", "gblur=sigma=50")
        self.assertEqual(both[both.index("-c:v") + 1], "libx264")
        self.assertEqual(both[both.index("-c:a") + 1], "aac")

    def test_video_bitrate_is_capped_below_the_source(self):
        self.app.compress_render_var.set(False)
        args = self.app._build_codec_args(
            "", "gblur=sigma=50", {"video": 1_158_823, "audio": 96_197}
        )
        expected = int(1_158_823 * BITRATE_SAFETY_FACTOR)
        self.assertLess(expected, 1_158_823)
        self.assertEqual(args[args.index("-maxrate") + 1], str(expected))
        self.assertEqual(args[args.index("-bufsize") + 1], str(expected * 2))
        self.assertEqual(args[args.index("-crf") + 1], "23")

    def test_audio_bitrate_never_exceeds_the_source(self):
        self.app.compress_render_var.set(True)
        low = self.app._build_codec_args("volume=0", "", {"video": None, "audio": 96_197})
        self.assertEqual(low[low.index("-b:a") + 1], str(int(96_197 * BITRATE_SAFETY_FACTOR)))

        high = self.app._build_codec_args("volume=0", "", {"video": None, "audio": 320_000})
        self.assertEqual(high[high.index("-b:a") + 1], "192000")

    def test_unknown_bitrate_omits_the_cap(self):
        args = self.app._build_codec_args("", "gblur=sigma=50", {"video": None, "audio": None})
        self.assertNotIn("-maxrate", args)
        self.assertNotIn("-bufsize", args)

    def test_render_and_mute_are_unselected_by_default(self):
        self.assertEqual(COMPRESSED_RENDER_HINT, "Renderizar y comprimir video de salida")
        self.assertFalse(self.app.compress_render_var.get())
        self.assertFalse(self.app.rows[0].mute_var.get())
        self.assertFalse(self.app.rows[0].blur_var.get())
        self.app.add_row()
        self.assertFalse(self.app.rows[-1].mute_var.get())
        self.assertFalse(self.app.rows[-1].blur_var.get())

    def test_compressed_render_forces_mp4_settings(self):
        self.app.compress_render_var.set(True)
        args = self.app._build_codec_args("", "")

        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-crf") + 1], "20")
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-b:a") + 1], "192000")
        self.assertEqual(args[args.index("-movflags") + 1], "+faststart")
        self.assertNotIn("copy", args)

    def test_compressed_render_rewrites_output_extension(self):
        self.app.output_path.set(r"C:\videos\clip.mkv")
        self.app.compress_render_var.set(True)
        self.app._on_compressed_render_toggle()
        self.assertEqual(self.app.output_path.get(), r"C:\videos\clip.mp4")

    def _process_and_wait(self, timeout=120):
        with patch.object(messagebox, "showinfo"), patch.object(messagebox, "showerror") as showerror:
            self.app.process_video()
            deadline = time.monotonic() + timeout
            while not self.app.status_var.get().startswith("Completado:"):
                self.app.update()
                self.assertFalse(showerror.called, self.app.status_var.get())
                self.assertLess(time.monotonic(), deadline, self.app.status_var.get())
                time.sleep(0.05)
        self.assertFalse(self.app.is_processing)
        self.assertFalse(self.app.process_button.instate(["disabled"]))

    @staticmethod
    def _make_source(path):
        run(
            "ffmpeg", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=10:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", path,
        )

    @staticmethod
    def _make_compressed_source(path):
        run(
            "ffmpeg", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-crf", "30", "-preset", "medium",
            "-c:a", "aac", "-b:a", "96k", "-shortest", path,
        )

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
    def test_blur_output_does_not_grow_beyond_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            result = os.path.join(directory, "result.mp4")
            self._make_compressed_source(source)

            self.app.video_path.set(source)
            self.app.output_path.set(result)
            self._set_rows([("2", "4", False, True)])
            self._process_and_wait()

            source_size = os.path.getsize(source)
            result_size = os.path.getsize(result)
            self.assertLessEqual(
                result_size, source_size,
                f"blur output grew: {source_size} -> {result_size}",
            )
            self.assertIn("Tamaño:", self.app.status_var.get())

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
    def test_compressed_render_reduces_source_size(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            result = os.path.join(directory, "result.mp4")
            self._make_compressed_source(source)

            self.app.video_path.set(source)
            self.app.output_path.set(result)
            self.app.compress_render_var.set(True)
            self._set_rows([("2", "4", True, True)])
            self._process_and_wait()

            source_size = os.path.getsize(source)
            result_size = os.path.getsize(result)
            self.assertLess(
                result_size, source_size,
                f"compressed render grew: {source_size} -> {result_size}",
            )

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
    def test_compressed_render_works_without_mute_or_blur(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            result = os.path.join(directory, "result.mp4")
            self._make_compressed_source(source)

            self.app.video_path.set(source)
            self.app.output_path.set(result)
            self.app.compress_render_var.set(True)
            self._set_rows([("", "", False, False)])
            self._process_and_wait()

            self.assertTrue(os.path.isfile(result))
            self.assertLess(os.path.getsize(result), os.path.getsize(source))

    @staticmethod
    def _frame(path, seconds):
        return run(
            "ffmpeg", "-loglevel", "error", "-ss", str(seconds), "-i", path,
            "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
        )

    @classmethod
    def _frame_difference(cls, first, second, seconds):
        before, after = cls._frame(first, seconds), cls._frame(second, seconds)
        return sum(abs(a - b) for a, b in zip(before, after)) / len(before)

    @staticmethod
    def _audio_peak(path, start, end):
        output = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-ss", str(start), "-to", str(end),
                "-i", path, "-map", "0:a:0", "-af", "volumedetect",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, check=True,
        ).stderr
        for line in output.splitlines():
            if "max_volume:" in line:
                return float(line.split("max_volume:")[1].strip().split()[0])
        raise AssertionError(f"volumedetect did not report max_volume for {path}")

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
    def test_render_applies_mute_and_blur_only_in_their_own_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            result = os.path.join(directory, "result.mp4")
            self._make_source(source)

            self.app.video_path.set(source)
            self.app.output_path.set(result)
            self._set_rows([
                ("1", "2", True, False),
                ("3", "3.8", False, True),
            ])
            self._process_and_wait()

            self.assertEqual(self.app.progress_text_var.get(), "100%")

            self.assertLess(self._audio_peak(result, 1.2, 1.8), -60.0)
            self.assertGreater(self._audio_peak(result, 0.1, 0.8), -20.0)

            self.assertGreater(self._frame_difference(source, result, 3.4), 10.0)
            self.assertLess(self._frame_difference(source, result, 0.5), 5.0)

    @unittest.skipUnless(HAS_FFMPEG, "FFmpeg required")
    def test_compressed_render_produces_h264_aac_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "source.mp4")
            self._make_source(source)

            self.app.video_path.set(source)
            self.app.output_path.set(os.path.join(directory, "result.mkv"))
            self.app.compress_render_var.set(True)
            self._set_rows([("1", "2", True, True)])
            self._process_and_wait()

            rendered = os.path.join(directory, "result.mp4")
            self.assertEqual(self.app.output_path.get(), rendered)
            self.assertTrue(os.path.isfile(rendered))

            codecs = run(
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1", rendered,
            ).decode().split()
            self.assertIn("h264", codecs)
            self.assertIn("aac", codecs)


if __name__ == "__main__":
    unittest.main()

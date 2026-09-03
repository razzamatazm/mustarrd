"""
Comskip hardware-decode mode (issue #429).

A global setting picks how Comskip decodes video while detecting commercials:
  none     -> no extra flag (today's behaviour, byte-for-byte)
  hwassist -> --hwassist
  nvidia   -> --cuvid

The flag is best-effort. If Comskip exits non-zero on the first attempt while a
hardware flag was passed, it is retried once with software decode before the
failure is treated as real, so an unsupported flag can never lose a recording.
"""
import asyncio
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_hw_decode_test", MODULE_PATH)
PP_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(PP_MODULE)

PostProcessor = PP_MODULE.PostProcessor


def make_proc(returncode: int, stderr: bytes = b""):
    proc = MagicMock()
    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    err = asyncio.StreamReader()
    if stderr:
        err.feed_data(stderr)
    err.feed_eof()
    proc.stdout = stdout
    proc.stderr = err
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class ComskipHwDecodeFlagTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.video = Path(self.tmp) / "Show.mkv"
        self.video.write_bytes(b"\x00" * 64)
        self.logs = []

    async def _log(self, message):
        self.logs.append(message)

    def _processor(self):
        processor = PostProcessor()
        processor._comskip_path = "/usr/bin/comskip"
        processor._ffmpeg_path = "/usr/bin/ffmpeg"
        return processor

    async def _run(self, mode, procs):
        processor = self._processor()
        exec_mock = AsyncMock(side_effect=procs)
        with patch.object(
            type(processor), "comskip_available",
            new_callable=PropertyMock, return_value=True,
        ), patch.object(
            type(processor), "ffmpeg_available",
            new_callable=PropertyMock, return_value=True,
        ), patch("asyncio.create_subprocess_exec", new=exec_mock):
            result = await processor.detect_commercials(
                str(self.video),
                log_callback=self._log,
                hw_decode_mode=mode,
            )
        return result, [list(call.args) for call in exec_mock.call_args_list]

    async def test_mode_none_passes_no_hardware_flag(self):
        _, commands = await self._run("none", [make_proc(0)])
        self.assertEqual(len(commands), 1)
        self.assertNotIn("--hwassist", commands[0])
        self.assertNotIn("--cuvid", commands[0])

    async def test_missing_mode_behaves_like_none(self):
        processor = self._processor()
        exec_mock = AsyncMock(side_effect=[make_proc(0)])
        with patch.object(
            type(processor), "comskip_available",
            new_callable=PropertyMock, return_value=True,
        ), patch("asyncio.create_subprocess_exec", new=exec_mock):
            await processor.detect_commercials(str(self.video))
        cmd = list(exec_mock.call_args_list[0].args)
        self.assertNotIn("--hwassist", cmd)
        self.assertNotIn("--cuvid", cmd)

    async def test_hwassist_mode_passes_hwassist_flag_only(self):
        _, commands = await self._run("hwassist", [make_proc(0)])
        self.assertIn("--hwassist", commands[0])
        self.assertNotIn("--cuvid", commands[0])

    async def test_nvidia_mode_passes_cuvid_flag_only(self):
        _, commands = await self._run("nvidia", [make_proc(0)])
        self.assertIn("--cuvid", commands[0])
        self.assertNotIn("--hwassist", commands[0])

    async def test_unknown_mode_is_treated_as_none(self):
        _, commands = await self._run("quicksync", [make_proc(0)])
        self.assertNotIn("--hwassist", commands[0])
        self.assertNotIn("--cuvid", commands[0])

    async def test_chosen_mode_appears_in_the_logged_command(self):
        await self._run("nvidia", [make_proc(0)])
        cmd_lines = [line for line in self.logs if "cmd:" in line]
        self.assertTrue(any("--cuvid" in line for line in cmd_lines), self.logs)

    async def test_mode_none_leaves_the_log_line_unchanged(self):
        await self._run("none", [make_proc(0)])
        cmd_lines = [line for line in self.logs if "cmd:" in line]
        self.assertTrue(cmd_lines)
        for line in cmd_lines:
            self.assertNotIn("hardware decode", line)

    async def test_hardware_failure_retries_once_with_software_decode(self):
        procs = [
            make_proc(3, b"comskip: unknown option --cuvid\n"),
            make_proc(0),
        ]
        result, commands = await self._run("nvidia", procs)
        self.assertEqual(len(commands), 2, "Comskip should be retried without the flag")
        self.assertIn("--cuvid", commands[0])
        self.assertNotIn("--cuvid", commands[1])
        self.assertTrue(
            any("software" in line.lower() for line in self.logs),
            f"A fallback warning belongs in the download log: {self.logs}",
        )
        # A successful software retry must not raise; no EDL on disk means None.
        self.assertIsNone(result)

    async def test_software_run_is_not_retried_twice(self):
        processor = self._processor()
        procs = [make_proc(3, b"boom\n")]
        exec_mock = AsyncMock(side_effect=procs)
        with patch.object(
            type(processor), "comskip_available",
            new_callable=PropertyMock, return_value=True,
        ), patch("asyncio.create_subprocess_exec", new=exec_mock):
            with self.assertRaises(Exception):
                await processor.detect_commercials(
                    str(self.video), log_callback=self._log, hw_decode_mode="none"
                )
        self.assertEqual(len(exec_mock.call_args_list), 1)

    async def test_no_commercials_exit_code_is_not_treated_as_hardware_failure(self):
        # Comskip signals "no commercials found" with a non-zero exit; that must
        # not trigger the software retry.
        proc = make_proc(1, b"Commercials were not found.\n")
        result, commands = await self._run("hwassist", [proc])
        self.assertEqual(len(commands), 1)
        self.assertIsNone(result)


class ComskipHwDecodeModeCatalogTests(unittest.TestCase):

    def test_catalog_always_offers_all_three_modes(self):
        processor = PostProcessor()
        with patch.object(processor, "get_available_hardware_accels", return_value=[
            {"id": "cpu", "name": "CPU (Software)", "available": True},
        ]):
            modes = processor.get_comskip_hw_decode_modes()
        self.assertEqual([m["id"] for m in modes], ["none", "hwassist", "nvidia"])
        by_id = {m["id"]: m for m in modes}
        self.assertTrue(by_id["none"]["available"])
        self.assertFalse(by_id["nvidia"]["available"])

    def test_nvidia_available_when_nvenc_is_detected(self):
        processor = PostProcessor()
        with patch.object(processor, "get_available_hardware_accels", return_value=[
            {"id": "cpu", "available": True},
            {"id": "nvenc", "available": True},
        ]):
            by_id = {m["id"]: m for m in processor.get_comskip_hw_decode_modes()}
        self.assertTrue(by_id["nvidia"]["available"])
        self.assertFalse(by_id["hwassist"]["available"])

    def test_hwassist_available_when_vaapi_is_detected(self):
        processor = PostProcessor()
        with patch.object(processor, "get_available_hardware_accels", return_value=[
            {"id": "cpu", "available": True},
            {"id": "vaapi", "available": True},
        ]):
            by_id = {m["id"]: m for m in processor.get_comskip_hw_decode_modes()}
        self.assertTrue(by_id["hwassist"]["available"])


if __name__ == "__main__":
    unittest.main()

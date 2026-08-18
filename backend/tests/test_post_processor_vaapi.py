import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "services" / "post_processor.py"
SPEC = importlib.util.spec_from_file_location("post_processor_under_test", MODULE_PATH)
POST_PROCESSOR_MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(POST_PROCESSOR_MODULE)

HardwareAccel = POST_PROCESSOR_MODULE.HardwareAccel
PostProcessor = POST_PROCESSOR_MODULE.PostProcessor


class VaapiDriverResolutionTests(unittest.TestCase):
    def setUp(self):
        self.processor = PostProcessor()
        self.processor._resolve_vaapi_driver.cache_clear()

    def tearDown(self):
        self.processor._resolve_vaapi_driver.cache_clear()

    def _make_render_device(self, kernel_driver: str):
        """Return (device_path, sysfs_class_dir) with a fake sysfs tree.

        Mirrors real-kernel layout:
          /sys/class/drm/renderD128  ->  sysfs entry dir
          sysfs_entry/device/driver  ->  drivers/<kernel_driver>
          sysfs_entry/device/vendor  (text file)
        """
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)

        device = root / "dev" / "dri" / "renderD128"
        device.parent.mkdir(parents=True, exist_ok=True)
        device.touch()

        sysfs_entry = root / "sysfs" / "renderD128"
        (sysfs_entry / "device").mkdir(parents=True, exist_ok=True)
        (sysfs_entry / "device" / "vendor").write_text("0x1002")

        driver_target = root / "drivers" / kernel_driver
        driver_target.mkdir(parents=True, exist_ok=True)
        (sysfs_entry / "device" / "driver").symlink_to(driver_target)

        sysfs_class_dir = root / "sys" / "class" / "drm"
        sysfs_class_dir.mkdir(parents=True, exist_ok=True)
        (sysfs_class_dir / "renderD128").symlink_to(sysfs_entry)

        return device, sysfs_class_dir

    def test_render_device_environment_override(self):
        with patch.dict(
            os.environ, {"CATCHUP_VAAPI_RENDER_DEVICE": "/dev/dri/renderD129"}, clear=False
        ):
            self.assertEqual(
                PostProcessor.resolve_render_device(),
                Path("/dev/dri/renderD129"),
            )

    def test_blank_render_device_environment_falls_back_to_default(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ, {"CATCHUP_VAAPI_RENDER_DEVICE": value}, clear=False
                ):
                    self.assertEqual(
                        PostProcessor.resolve_render_device(),
                        Path("/dev/dri/renderD128"),
                    )

    def test_configured_device_used_when_env_unset(self):
        self.assertEqual(
            PostProcessor.resolve_render_device("/dev/dri/renderD130"),
            Path("/dev/dri/renderD130"),
        )

    def test_environment_beats_configured_device(self):
        """The env var is the headless override, so it has to win over Settings."""
        with patch.dict(
            os.environ, {"CATCHUP_VAAPI_RENDER_DEVICE": "/dev/dri/renderD129"}, clear=False
        ):
            self.assertEqual(
                PostProcessor.resolve_render_device("/dev/dri/renderD130"),
                Path("/dev/dri/renderD129"),
            )
            self.assertTrue(PostProcessor.render_device_is_locked())

    def test_blank_configured_device_falls_back_to_default(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertEqual(
                    PostProcessor.resolve_render_device(value),
                    Path("/dev/dri/renderD128"),
                )

    def test_env_override_wins(self):
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.dict(os.environ, {"LIBVA_DRIVER_NAME": "custom-driver"}, clear=False):
                details = self.processor.get_vaapi_diagnostics()

        self.assertTrue(details["enabled"])
        self.assertEqual(details["driver"], "custom-driver")
        self.assertEqual(details["source"], "env")

    def test_amd_kernel_driver_maps_to_radeonsi(self):
        device, sysfs_class = self._make_render_device("amdgpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_DEFAULT_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    details = self.processor.get_vaapi_diagnostics()

        self.assertEqual(details["driver"], "radeonsi")
        self.assertEqual(details["source"], "auto-detected")
        self.assertEqual(details["kernel_driver"], "amdgpu")

    def test_intel_kernel_driver_maps_to_ihd(self):
        device, sysfs_class = self._make_render_device("xe")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_DEFAULT_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    details = self.processor.get_vaapi_diagnostics()

        self.assertEqual(details["driver"], "iHD")
        self.assertEqual(details["source"], "auto-detected")
        self.assertEqual(details["kernel_driver"], "xe")

    def test_unknown_kernel_driver_leaves_driver_unset(self):
        device, sysfs_class = self._make_render_device("mysterygpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_DEFAULT_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    details = self.processor.get_vaapi_diagnostics()

        self.assertIsNone(details["driver"])
        self.assertEqual(details["source"], "auto")
        self.assertEqual(details["kernel_driver"], "mysterygpu")

    def test_vaapi_env_uses_detected_driver(self):
        device, sysfs_class = self._make_render_device("amdgpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_DEFAULT_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    env = self.processor._build_ffmpeg_env(HardwareAccel.VAAPI)

        self.assertEqual(env["LIBVA_DRIVER_NAME"], "radeonsi")
        self.assertIn("LIBVA_DRIVERS_PATH", env)

    def test_vaapi_env_does_not_force_unknown_driver(self):
        device, sysfs_class = self._make_render_device("mysterygpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_DEFAULT_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    env = self.processor._build_ffmpeg_env(HardwareAccel.VAAPI)

        self.assertNotIn("LIBVA_DRIVER_NAME", env)
        self.assertIn("LIBVA_DRIVERS_PATH", env)

    def test_non_vaapi_env_does_not_modify_driver_override(self):
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.dict(os.environ, {"LIBVA_DRIVER_NAME": "keep-me"}, clear=False):
                env = self.processor._build_ffmpeg_env(HardwareAccel.CPU)

        self.assertEqual(env["LIBVA_DRIVER_NAME"], "keep-me")


class VaapiDeviceArgsTests(unittest.TestCase):
    """The args that actually reach ffmpeg, per the configured render node."""

    def setUp(self):
        self.processor = PostProcessor()

    def test_vaapi_emits_configured_device(self):
        args = self.processor._vaapi_device_args(
            HardwareAccel.VAAPI, Path("/dev/dri/renderD129")
        )
        self.assertEqual(args, ["-vaapi_device", "/dev/dri/renderD129"])

    def test_non_vaapi_accels_emit_nothing(self):
        for accel in (HardwareAccel.CPU, HardwareAccel.NVIDIA, HardwareAccel.AMD):
            with self.subTest(accel=accel):
                self.assertEqual(
                    self.processor._vaapi_device_args(accel, Path("/dev/dri/renderD128")),
                    [],
                )

    def test_no_hardcoded_render_node_in_command_builders(self):
        """Regression guard: every ffmpeg path must go through the resolver.

        The bug this whole feature exists to fix was four hardcoded renderD128
        literals. Only the documented default may name a node directly.
        """
        source = MODULE_PATH.read_text()
        occurrences = source.count("/dev/dri/renderD128")
        self.assertEqual(
            occurrences,
            1,
            "expected exactly one /dev/dri/renderD128 literal "
            "(VAAPI_DEFAULT_RENDER_DEVICE), found %d" % occurrences,
        )


class RenderDeviceEnumerationTests(unittest.TestCase):
    """Feeding the Settings picker."""

    def setUp(self):
        self.processor = PostProcessor()

    def _make_dri_root(self, nodes):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)

        dri = root / "dev" / "dri"
        dri.mkdir(parents=True, exist_ok=True)
        sysfs_class_dir = root / "sys" / "class" / "drm"
        sysfs_class_dir.mkdir(parents=True, exist_ok=True)

        for name, kernel_driver in nodes.items():
            (dri / name).touch()
            if kernel_driver is None:
                continue
            sysfs_entry = root / "sysfs" / name
            (sysfs_entry / "device").mkdir(parents=True, exist_ok=True)
            (sysfs_entry / "device" / "vendor").write_text("0x8086")
            driver_target = root / "drivers" / kernel_driver
            driver_target.mkdir(parents=True, exist_ok=True)
            (sysfs_entry / "device" / "driver").symlink_to(driver_target)
            (sysfs_class_dir / name).symlink_to(sysfs_entry)

        return dri, sysfs_class_dir

    def _list(self, dri, sysfs_class):
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_DEVICE_ROOT", dri):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    return self.processor.list_render_devices()

    def test_lists_every_render_node(self):
        dri, sysfs_class = self._make_dri_root(
            {"renderD128": "i915", "renderD129": "amdgpu", "card0": "i915"}
        )
        devices = self._list(dri, sysfs_class)

        self.assertEqual(
            [d["device"] for d in devices],
            [str(dri / "renderD128"), str(dri / "renderD129")],
            "card nodes are not render nodes and must not be offered",
        )
        self.assertEqual(devices[0]["driver"], "iHD")
        self.assertEqual(devices[1]["driver"], "radeonsi")
        self.assertEqual(devices[1]["kernel_driver"], "amdgpu")

    def test_unidentifiable_node_is_still_listed(self):
        """A node we cannot probe is still selectable; the user may know better."""
        dri, sysfs_class = self._make_dri_root({"renderD128": None})
        devices = self._list(dri, sysfs_class)

        self.assertEqual(len(devices), 1)
        self.assertIsNone(devices[0]["driver"])
        self.assertIsNone(devices[0]["kernel_driver"])

    def test_no_devices_off_linux(self):
        dri, sysfs_class = self._make_dri_root({"renderD128": "i915"})
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Darwin"):
            with patch.object(PostProcessor, "VAAPI_DEVICE_ROOT", dri):
                self.assertEqual(self.processor.list_render_devices(), [])

    def test_missing_dri_directory_is_not_an_error(self):
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(
                PostProcessor, "VAAPI_DEVICE_ROOT", Path("/nonexistent/dri")
            ):
                self.assertEqual(self.processor.list_render_devices(), [])


if __name__ == "__main__":
    unittest.main()

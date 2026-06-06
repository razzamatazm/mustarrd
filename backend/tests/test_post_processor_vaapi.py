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
            with patch.object(PostProcessor, "VAAPI_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    details = self.processor.get_vaapi_diagnostics()

        self.assertEqual(details["driver"], "radeonsi")
        self.assertEqual(details["source"], "auto-detected")
        self.assertEqual(details["kernel_driver"], "amdgpu")

    def test_intel_kernel_driver_maps_to_ihd(self):
        device, sysfs_class = self._make_render_device("xe")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    details = self.processor.get_vaapi_diagnostics()

        self.assertEqual(details["driver"], "iHD")
        self.assertEqual(details["source"], "auto-detected")
        self.assertEqual(details["kernel_driver"], "xe")

    def test_unknown_kernel_driver_leaves_driver_unset(self):
        device, sysfs_class = self._make_render_device("mysterygpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    details = self.processor.get_vaapi_diagnostics()

        self.assertIsNone(details["driver"])
        self.assertEqual(details["source"], "auto")
        self.assertEqual(details["kernel_driver"], "mysterygpu")

    def test_vaapi_env_uses_detected_driver(self):
        device, sysfs_class = self._make_render_device("amdgpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    env = self.processor._build_ffmpeg_env(HardwareAccel.VAAPI)

        self.assertEqual(env["LIBVA_DRIVER_NAME"], "radeonsi")
        self.assertIn("LIBVA_DRIVERS_PATH", env)

    def test_vaapi_env_does_not_force_unknown_driver(self):
        device, sysfs_class = self._make_render_device("mysterygpu")
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.object(PostProcessor, "VAAPI_RENDER_DEVICE", device):
                with patch.object(PostProcessor, "VAAPI_SYSFS_DRM_CLASS", sysfs_class):
                    env = self.processor._build_ffmpeg_env(HardwareAccel.VAAPI)

        self.assertNotIn("LIBVA_DRIVER_NAME", env)
        self.assertIn("LIBVA_DRIVERS_PATH", env)

    def test_non_vaapi_env_does_not_modify_driver_override(self):
        with patch.object(POST_PROCESSOR_MODULE.platform, "system", return_value="Linux"):
            with patch.dict(os.environ, {"LIBVA_DRIVER_NAME": "keep-me"}, clear=False):
                env = self.processor._build_ffmpeg_env(HardwareAccel.CPU)

        self.assertEqual(env["LIBVA_DRIVER_NAME"], "keep-me")


if __name__ == "__main__":
    unittest.main()

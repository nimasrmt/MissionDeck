"""Locate an existing Orca CLI without launching it or changing the machine."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import _resolve_orca_exe


class OrcaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.exe = self.root / "Programs" / "Orca" / "resources" / "bin" / "orca.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.touch()
        for context in (patch.dict("os.environ", {"LOCALAPPDATA": str(self.root)}, clear=True),
                        patch("app.config.shutil.which", return_value=None),
                        patch("app.config.sys.platform", "win32")):
            context.start()
            self.addCleanup(context.stop)

    def test_find_existing_standard_windows_install_outside_path(self):
        self.assertEqual(_resolve_orca_exe(), self.exe)

    def test_explicit_override_takes_precedence(self):
        custom = str(self.root / "custom" / "orca.exe")
        with patch.dict("os.environ", {"MISSIONDECK_ORCA_EXE": custom}):
            self.assertEqual(_resolve_orca_exe(), Path(custom))

    def test_path_takes_precedence_over_standard_install(self):
        custom = str(self.root / "path" / "orca.exe")
        with patch("app.config.shutil.which", return_value=custom):
            self.assertEqual(_resolve_orca_exe(), Path(custom))

    def test_non_windows_does_not_use_windows_install(self):
        with patch("app.config.sys.platform", "linux"):
            self.assertEqual(_resolve_orca_exe(), Path("orca"))

    def test_missing_executable_keeps_unavailable_fallback(self):
        self.exe.unlink()
        self.assertEqual(_resolve_orca_exe(), Path("orca"))

    def test_missing_local_app_data_keeps_unavailable_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_resolve_orca_exe(), Path("orca"))

    def test_relative_local_app_data_is_not_trusted(self):
        with patch.dict("os.environ", {"LOCALAPPDATA": "."}):
            self.assertEqual(_resolve_orca_exe(), Path("orca"))


if __name__ == "__main__":
    unittest.main()

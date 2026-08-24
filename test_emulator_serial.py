#!/usr/bin/env python3
import os
import tempfile
import unittest
from unittest import mock

from emulator_serial import emulator_tty_path


class EmulatorSerialTests(unittest.TestCase):
    def test_environment_override_is_used_when_present(self):
        with tempfile.NamedTemporaryFile() as endpoint:
            with mock.patch.dict(os.environ, {"TARGET_SERIALIO_USB_TTY": endpoint.name}):
                self.assertEqual(emulator_tty_path(), endpoint.name)

    def test_missing_override_is_not_reported(self):
        missing = "/tmp/s100-host-link-definitely-missing"
        try:
            os.unlink(missing)
        except FileNotFoundError:
            pass
        with mock.patch.dict(os.environ, {"TARGET_SERIALIO_USB_TTY": missing}):
            self.assertEqual(emulator_tty_path(), "")

    def test_default_path_tracks_current_uid(self):
        if os.name != "posix" or not hasattr(os, "getuid"):
            self.skipTest("POSIX-only default path")
        expected = f"/tmp/targets100sim-usb-{os.getuid()}"
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "emulator_serial.os.path.exists", side_effect=lambda path: path == expected
        ):
            self.assertEqual(emulator_tty_path(), expected)


if __name__ == "__main__":
    unittest.main()

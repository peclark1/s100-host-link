#!/usr/bin/env python3
"""Launch the production Host Link UI with CP/M editing and targetsim discovery."""
from __future__ import annotations

import launch_editor as production
from emulator_serial import emulator_tty_path

ui = production.base.ui
_ORIGINAL_REFRESH_PORTS = ui.Win.refresh_ports


def _refresh_ports_with_targetsim(self):
    old_device = self.port() or str(self.cfg.get("last_port", ""))
    entries = []

    emulator = emulator_tty_path()
    if emulator:
        entries.append((emulator, f"{emulator} — IMSAI target emulator Serial I/O USB"))

    if ui.list_ports is not None:
        for port in ui.list_ports.comports():
            label = port.device
            description = (port.description or "").strip()
            if description and description.lower() != "n/a":
                label = f"{port.device} — {description}"
            if port.device not in [device for device, _label in entries]:
                entries.append((port.device, label))

    if old_device and old_device not in [device for device, _label in entries]:
        entries.append((old_device, f"{old_device} — saved device (not currently detected)"))

    while self.pm.get_n_items():
        self.pm.remove(0)
    self.ports = []
    for device, label in entries:
        self.pm.append(label)
        self.ports.append(device)

    if old_device in self.ports:
        selected = self.ports.index(old_device)
    elif emulator and emulator in self.ports:
        selected = self.ports.index(emulator)
    elif self.ports:
        selected = 0
    else:
        selected = ui.Gtk.INVALID_LIST_POSITION
    self.pdd.set_selected(selected)


ui.Win.refresh_ports = _refresh_ports_with_targetsim

if __name__ == "__main__":
    ui.Adw.init()
    raise SystemExit(ui.App().run(None))

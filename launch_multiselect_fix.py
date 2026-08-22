#!/usr/bin/env python3
"""Host Link multi-file selection using explicit per-file checkboxes.

Gtk.ListBox selection and row drag behavior varies across GTK/PyGObject builds.
On the target Ubuntu system several rows can be painted as selected while the
selection APIs still report only the drag-origin row. Avoid that ambiguity by
making batch membership explicit: check the files to copy, then drag any one
of the checked files to the other pane.

Dragging an unchecked file remains a normal one-file transfer.

The original dual-pane UI keyed Linux rows by Python id(row). PyGObject may
recycle those temporary wrapper objects, causing dictionary entries to be
silently overwritten. Rebuild the Linux row mapping by stable ListBox row
index after every refresh so every visible filesystem entry remains addressable.
"""
from __future__ import annotations

from pathlib import Path

import launch_resizable as multi

Gtk = multi.Gtk

_ORIGINAL_MULTI_INIT = multi._resizable_init
_ORIGINAL_LREFRESH = multi.base.ui.Win.lrefresh
_ORIGINAL_CRENDER = multi.base.ui.Win.crender


def _linux_key(path: Path) -> str:
    return str(Path(path))


def _cpm_key(name: str) -> str:
    return str(name).upper()


def _scan_linux_entries(self):
    """Return Linux directory entries in exactly the UI's display order."""
    try:
        return sorted(
            self.ldir.iterdir(),
            key=lambda path: (not path.is_dir(), path.name.casefold()),
        )
    except OSError:
        return []


def _rebuild_linux_row_map(self):
    """Replace fragile id(row) keys with one stable entry per ListBox row."""
    entries = _scan_linux_entries(self)
    self.lrows = {index: path for index, path in enumerate(entries)}
    return entries


def _linux_checked_paths(self):
    """Return checked Linux files in visible row order."""
    wanted = set(getattr(self, "_linux_checked", set()))
    result = []
    for path in self.lrows.values():
        path = Path(path)
        if path.is_file() and _linux_key(path) in wanted:
            result.append(path)
    return result


def _cpm_checked_files(self):
    """Return checked CP/M files in visible row order."""
    wanted = set(getattr(self, "_cpm_checked", set()))
    return [item for item in self.cfiles if _cpm_key(item.name) in wanted]


def _set_linux_checked(self, button, path: Path):
    key = _linux_key(path)
    if button.get_active():
        self._linux_checked.add(key)
    else:
        self._linux_checked.discard(key)
    count = len(self._linux_checked)
    self.status.set_text(
        "Ready" if count == 0 else f"{count} Linux file(s) selected for copy"
    )
    if count:
        self.log(f"Linux batch selection: {count} file(s)")


def _set_cpm_checked(self, button, item):
    key = _cpm_key(item.name)
    if button.get_active():
        self._cpm_checked.add(key)
    else:
        self._cpm_checked.discard(key)
    count = len(self._cpm_checked)
    self.status.set_text(
        "Ready" if count == 0 else f"{count} CP/M file(s) selected for copy"
    )
    if count:
        self.log(f"CP/M batch selection: {count} file(s)")


def _prepend_checkbox(row, *, active=False, tooltip=""):
    box = row.get_child()
    if not isinstance(box, Gtk.Box):
        return None
    button = Gtk.CheckButton()
    button.set_active(active)
    button.set_valign(Gtk.Align.CENTER)
    if tooltip:
        button.set_tooltip_text(tooltip)
    box.prepend(button)
    return button


def _linux_refresh(self):
    """Refresh Linux pane, repair row mapping, and checkbox every file."""
    self._linux_checked = set()
    self._linux_checkbuttons = {}
    result = _ORIGINAL_LREFRESH(self)

    # _ORIGINAL_LREFRESH stores entries using id(Gtk.ListBoxRow). Those Python
    # wrapper ids are not stable/unique for the lifetime of the GTK rows. Scan
    # the directory again in the same sort order and map each path to its
    # durable ListBox row index instead.
    entries = _rebuild_linux_row_map(self)
    file_count = 0

    for index, path in enumerate(entries):
        row = self.ll.get_row_at_index(index)
        if row is None:
            continue
        path = Path(path)
        if not path.is_file():
            continue

        button = _prepend_checkbox(
            row,
            tooltip="Include this file in a multi-file transfer",
        )
        if button is None:
            continue

        key = _linux_key(path)
        self._linux_checkbuttons[key] = button
        button.connect("toggled", lambda b, p=path: _set_linux_checked(self, b, p))
        file_count += 1

    # One concise diagnostic makes it obvious that the entire directory was
    # decorated rather than only the subset formerly surviving in self.lrows.
    if hasattr(self, "buf"):
        self.log(f"Linux file list: {file_count} transferable file(s)")

    return result


def _cpm_render(self, files):
    """Render CP/M pane and add a checkbox beside each file."""
    self._cpm_checked = set()
    self._cpm_checkbuttons = {}
    result = _ORIGINAL_CRENDER(self, files)

    for index, item in enumerate(self.cfiles):
        row = self.cl.get_row_at_index(index)
        if row is None:
            continue
        button = _prepend_checkbox(
            row,
            tooltip="Include this file in a multi-file transfer",
        )
        if button is None:
            continue
        key = _cpm_key(item.name)
        self._cpm_checkbuttons[key] = button
        button.connect("toggled", lambda b, f=item: _set_cpm_checked(self, b, f))

    return result


def _clear_linux_checks(self):
    for button in getattr(self, "_linux_checkbuttons", {}).values():
        if button.get_active():
            button.set_active(False)
    self._linux_checked.clear()


def _clear_cpm_checks(self):
    for button in getattr(self, "_cpm_checkbuttons", {}).values():
        if button.get_active():
            button.set_active(False)
    self._cpm_checked.clear()


def _provider(self, value):
    """Keep drag payload simple; the destination resolves checked batch state."""
    return multi._ORIGINAL_PROVIDER(self, value)


def _drop_on_cpm(self, _target, value, _x, _y):
    """Copy one Linux file, or all checked Linux files, to CP/M."""
    if not isinstance(value, str) or not value.startswith(self.LD):
        return False

    dragged = Path(value[len(self.LD):])
    checked = _linux_checked_paths(self)
    dragged_is_checked = _linux_key(dragged) in self._linux_checked

    if dragged_is_checked and checked:
        paths = checked
        self.log(f"Dragging checked Linux batch: {len(paths)} file(s)")
    else:
        paths = [dragged]

    self.log(f"Drop: transferring {len(paths)} Linux file(s) to CP/M")
    started = multi._start_send_batch(self, paths)
    if started and dragged_is_checked:
        _clear_linux_checks(self)
    return started


def _drop_on_linux(self, _target, value, _x, _y):
    """Copy one CP/M file, or all checked CP/M files, to Linux."""
    if not isinstance(value, str) or not value.startswith(self.CD):
        return False

    dragged_name = value[len(self.CD):]
    checked = _cpm_checked_files(self)
    dragged_is_checked = _cpm_key(dragged_name) in self._cpm_checked

    if dragged_is_checked and checked:
        files = checked
        self.log(f"Dragging checked CP/M batch: {len(files)} file(s)")
    else:
        item = next(
            (entry for entry in self.cfiles if entry.name.upper() == dragged_name.upper()),
            None,
        )
        files = [item] if item is not None else []

    self.log(f"Drop: transferring {len(files)} CP/M file(s) to Linux")
    started = multi._start_receive_batch(self, files)
    if started and dragged_is_checked:
        _clear_cpm_checks(self)
    return started


def _fixed_init(self, app):
    self._linux_checked = set()
    self._cpm_checked = set()
    self._linux_checkbuttons = {}
    self._cpm_checkbuttons = {}

    _ORIGINAL_MULTI_INIT(self, app)

    # The checkbox is the authoritative batch selector. Keep ordinary row
    # selection single so a click/drag cannot create a second hidden selection
    # state that disagrees with the checkboxes.
    self.ll.set_selection_mode(Gtk.SelectionMode.SINGLE)
    self.cl.set_selection_mode(Gtk.SelectionMode.SINGLE)
    self.ll.set_tooltip_text(
        "Check multiple files, then drag any checked file to CP/M. "
        "Drag an unchecked file for a single-file copy."
    )
    self.cl.set_tooltip_text(
        "Check multiple files, then drag any checked file to Linux. "
        "Drag an unchecked file for a single-file copy."
    )
    self.log("Multi-file drag support active: checkbox batching v8")


# Install before the first window is created. The wrapped refresh/render methods
# insert checkboxes every time either directory is rebuilt.
multi._provider = _provider
multi.base.ui.Win.provider = _provider
multi.base.ui.Win.lrefresh = _linux_refresh
multi.base.ui.Win.crender = _cpm_render
multi.base.ui.Win.dropc = _drop_on_cpm
multi.base.ui.Win.dropl = _drop_on_linux
multi.base.ui.Win.__init__ = _fixed_init


if __name__ == "__main__":
    multi.base.ui.Adw.init()
    raise SystemExit(multi.base.ui.App().run(None))

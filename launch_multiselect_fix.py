#!/usr/bin/env python3
"""Start Host Link with reliable multi-file drag-and-drop batching.

Gtk.ListBox in MULTIPLE mode may collapse a selection to the row under the
pointer as a drag starts.  Do not depend on the drag payload to carry the whole
selection.  Instead, remember the multi-selection, capture it before GTK handles
the drag-start click when possible, and let the destination drop handler choose
the complete transfer set.
"""
from __future__ import annotations

from pathlib import Path

import launch_resizable as multi

Gtk = multi.Gtk

_ORIGINAL_MULTI_INIT = multi._resizable_init


def _linux_selected(self, _listbox, _row=None):
    paths = multi._linux_selected_paths(self)
    self.lsels = paths
    self.lsel = paths[0] if paths else None

    # Keep the last real multi-selection available in case GTK collapses the
    # ListBox selection while the drag is being initiated.
    if len(paths) > 1:
        self._linux_last_multi = list(paths)
        count = len(paths)
        if getattr(self, "_linux_last_logged_count", 0) != count:
            self.log(f"Linux selection: {count} files")
            self._linux_last_logged_count = count
    elif len(paths) == 0:
        self._linux_last_multi = []
        self._linux_last_logged_count = 0

    self.buttons()


def _cpm_selected(self, _listbox, _row=None):
    files = multi._cpm_selected_files(self)
    self.csels = files
    self.csel = files[0] if files else None

    if len(files) > 1:
        self._cpm_last_multi = list(files)
        count = len(files)
        if getattr(self, "_cpm_last_logged_count", 0) != count:
            self.log(f"CP/M selection: {count} files")
            self._cpm_last_logged_count = count
    elif len(files) == 0:
        self._cpm_last_multi = []
        self._cpm_last_logged_count = 0

    self.buttons()


def _capture_linux_press(self, _gesture, _n_press, _x, y):
    """Snapshot Linux selection before ListBox processes the drag-start press."""
    self._linux_press_seen = True
    self._linux_drag_snapshot = []

    row = self.ll.get_row_at_y(int(y))
    if row is None:
        return

    selected_rows = self.ll.get_selected_rows()
    selected_indices = {item.get_index() for item in selected_rows}
    if row.get_index() not in selected_indices or len(selected_rows) <= 1:
        return

    paths = []
    for selected in sorted(selected_rows, key=lambda item: item.get_index()):
        path = multi._linux_path_for_row(self, selected)
        if path is not None and path.is_file():
            paths.append(path)

    self._linux_drag_snapshot = paths
    if len(paths) > 1:
        self.log(f"Drag press captured: {len(paths)} Linux files")


def _capture_cpm_press(self, _gesture, _n_press, _x, y):
    """Snapshot CP/M selection before ListBox processes the drag-start press."""
    self._cpm_press_seen = True
    self._cpm_drag_snapshot = []

    row = self.cl.get_row_at_y(int(y))
    if row is None:
        return

    selected_rows = self.cl.get_selected_rows()
    selected_indices = {item.get_index() for item in selected_rows}
    if row.get_index() not in selected_indices or len(selected_rows) <= 1:
        return

    files = []
    for selected in sorted(selected_rows, key=lambda item: item.get_index()):
        index = selected.get_index()
        if 0 <= index < len(self.cfiles):
            files.append(self.cfiles[index])

    self._cpm_drag_snapshot = files
    if len(files) > 1:
        self.log(f"Drag press captured: {len(files)} CP/M files")


def _install_press_capture(self):
    """Observe pointer presses before Gtk.ListBox changes selection."""
    linux_press = Gtk.GestureClick()
    linux_press.set_button(1)
    linux_press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    linux_press.connect(
        "pressed",
        lambda g, n, x, y: _capture_linux_press(self, g, n, x, y),
    )
    self.ll.add_controller(linux_press)
    self._linux_press_capture = linux_press

    cpm_press = Gtk.GestureClick()
    cpm_press.set_button(1)
    cpm_press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    cpm_press.connect(
        "pressed",
        lambda g, n, x, y: _capture_cpm_press(self, g, n, x, y),
    )
    self.cl.add_controller(cpm_press)
    self._cpm_press_capture = cpm_press


def _provider(self, value):
    """Use the normal one-file drag payload; batching is decided on drop."""
    return multi._ORIGINAL_PROVIDER(self, value)


def _linux_transfer_set(self, dragged: Path):
    """Choose the Linux files represented by this drag."""
    snapshot = list(getattr(self, "_linux_drag_snapshot", []))

    # If the capture-phase observer saw this press, its answer is authoritative.
    # An empty snapshot means the press began from a single/nonselected row.
    if getattr(self, "_linux_press_seen", False):
        if len(snapshot) > 1 and dragged in snapshot:
            return snapshot
        return [dragged]

    # Fallbacks are mainly diagnostic insurance for GTK/backend variations.
    current = multi._linux_selected_paths(self)
    if len(current) > 1 and dragged in current:
        return current

    previous = list(getattr(self, "_linux_last_multi", []))
    if len(previous) > 1 and dragged in previous:
        self.log("Using remembered Linux multi-selection for drag")
        return previous

    return [dragged]


def _cpm_transfer_set(self, dragged_name: str):
    """Choose the CP/M files represented by this drag."""
    key = dragged_name.upper()
    snapshot = list(getattr(self, "_cpm_drag_snapshot", []))

    if getattr(self, "_cpm_press_seen", False):
        if len(snapshot) > 1 and any(item.name.upper() == key for item in snapshot):
            return snapshot
        return [item for item in self.cfiles if item.name.upper() == key][:1]

    current = multi._cpm_selected_files(self)
    if len(current) > 1 and any(item.name.upper() == key for item in current):
        return current

    previous = list(getattr(self, "_cpm_last_multi", []))
    if len(previous) > 1 and any(item.name.upper() == key for item in previous):
        self.log("Using remembered CP/M multi-selection for drag")
        return previous

    return [item for item in self.cfiles if item.name.upper() == key][:1]


def _drop_on_cpm(self, _target, value, _x, _y):
    """Drop Linux file(s) onto CP/M, resolving batching at destination."""
    # Remain compatible with a batch payload produced by an older test build.
    batch = multi._decode_batch(value, self.LD)
    if batch is not None:
        paths = [Path(item) for item in batch]
    else:
        if not isinstance(value, str) or not value.startswith(self.LD):
            return False
        dragged = Path(value[len(self.LD):])
        paths = _linux_transfer_set(self, dragged)

    self._linux_press_seen = False
    self._linux_drag_snapshot = []
    self._linux_last_multi = []
    self._linux_last_logged_count = 0

    self.log(f"Drop: transferring {len(paths)} Linux file(s) to CP/M")
    return multi._start_send_batch(self, paths)


def _drop_on_linux(self, _target, value, _x, _y):
    """Drop CP/M file(s) onto Linux, resolving batching at destination."""
    batch = multi._decode_batch(value, self.CD)
    if batch is not None:
        by_name = {item.name.upper(): item for item in self.cfiles}
        files = [by_name[name.upper()] for name in batch if name.upper() in by_name]
    else:
        if not isinstance(value, str) or not value.startswith(self.CD):
            return False
        dragged_name = value[len(self.CD):]
        files = _cpm_transfer_set(self, dragged_name)

    self._cpm_press_seen = False
    self._cpm_drag_snapshot = []
    self._cpm_last_multi = []
    self._cpm_last_logged_count = 0

    self.log(f"Drop: transferring {len(files)} CP/M file(s) to Linux")
    return multi._start_receive_batch(self, files)


def _fixed_init(self, app):
    self._linux_drag_snapshot = []
    self._cpm_drag_snapshot = []
    self._linux_last_multi = []
    self._cpm_last_multi = []
    self._linux_press_seen = False
    self._cpm_press_seen = False
    self._linux_last_logged_count = 0
    self._cpm_last_logged_count = 0

    _ORIGINAL_MULTI_INIT(self, app)
    _install_press_capture(self)
    self.log("Multi-file drag support active: drop-side batching v4")


# Install patches before the first window is constructed so the DragSource and
# DropTarget signal connections made by Win.ui()/Win.dnd() bind to these methods.
multi._linux_selected = _linux_selected
multi._cpm_selected = _cpm_selected
multi._provider = _provider
multi.base.ui.Win.lselected = _linux_selected
multi.base.ui.Win.cselected = _cpm_selected
multi.base.ui.Win.provider = _provider
multi.base.ui.Win.dropc = _drop_on_cpm
multi.base.ui.Win.dropl = _drop_on_linux
multi.base.ui.Win.__init__ = _fixed_init


if __name__ == "__main__":
    multi.base.ui.Adw.init()
    raise SystemExit(multi.base.ui.App().run(None))

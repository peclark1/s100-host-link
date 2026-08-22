#!/usr/bin/env python3
"""Start Host Link with reliable multi-file drag selection preservation.

Gtk.ListBox in MULTIPLE mode can collapse a selection to the row under the
pointer when the same mouse press begins a drag.  By the time DragSource asks
for its content, get_selected_rows() may therefore contain only that row.

Capture the selected rows in the CAPTURE event phase, before Gtk.ListBox handles
the button press.  If the press is on one member of an existing multi-selection,
the drag provider uses that pre-click snapshot for the batch payload.
"""
from __future__ import annotations

import json
from pathlib import Path

import launch_resizable as multi

Gtk = multi.Gtk

_ORIGINAL_MULTI_INIT = multi._resizable_init


def _linux_selected(self, _listbox, _row=None):
    paths = multi._linux_selected_paths(self)
    self.lsels = paths
    self.lsel = paths[0] if paths else None
    self.buttons()


def _cpm_selected(self, _listbox, _row=None):
    files = multi._cpm_selected_files(self)
    self.csels = files
    self.csel = files[0] if files else None
    self.buttons()


def _capture_linux_press(self, _gesture, _n_press, _x, y):
    """Snapshot Linux selection before ListBox processes the drag-start press."""
    row = self.ll.get_row_at_y(int(y))
    if row is None:
        self._linux_drag_snapshot = []
        return

    selected_rows = self.ll.get_selected_rows()
    selected_indices = {item.get_index() for item in selected_rows}
    if row.get_index() not in selected_indices or len(selected_rows) <= 1:
        self._linux_drag_snapshot = []
        return

    paths = []
    for selected in sorted(selected_rows, key=lambda item: item.get_index()):
        path = multi._linux_path_for_row(self, selected)
        if path is not None and path.is_file():
            paths.append(path)
    self._linux_drag_snapshot = paths


def _capture_cpm_press(self, _gesture, _n_press, _x, y):
    """Snapshot CP/M selection before ListBox processes the drag-start press."""
    row = self.cl.get_row_at_y(int(y))
    if row is None:
        self._cpm_drag_snapshot = []
        return

    selected_rows = self.cl.get_selected_rows()
    selected_indices = {item.get_index() for item in selected_rows}
    if row.get_index() not in selected_indices or len(selected_rows) <= 1:
        self._cpm_drag_snapshot = []
        return

    files = []
    for selected in sorted(selected_rows, key=lambda item: item.get_index()):
        index = selected.get_index()
        if 0 <= index < len(self.cfiles):
            files.append(self.cfiles[index])
    self._cpm_drag_snapshot = files


def _install_press_capture(self):
    """Install mouse-press observers that run before Gtk.ListBox selection logic."""
    linux_press = Gtk.GestureClick()
    linux_press.set_button(1)
    linux_press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    linux_press.connect("pressed", lambda g, n, x, y: _capture_linux_press(self, g, n, x, y))
    self.ll.add_controller(linux_press)
    self._linux_press_capture = linux_press

    cpm_press = Gtk.GestureClick()
    cpm_press.set_button(1)
    cpm_press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    cpm_press.connect("pressed", lambda g, n, x, y: _capture_cpm_press(self, g, n, x, y))
    self.cl.add_controller(cpm_press)
    self._cpm_press_capture = cpm_press


def _provider(self, value):
    """Build drag content from the pre-click selection snapshot."""
    if isinstance(value, str) and value.startswith(self.LD):
        dragged = Path(value[len(self.LD):])
        snapshot = list(getattr(self, "_linux_drag_snapshot", []))
        if len(snapshot) > 1 and any(Path(item) == dragged for item in snapshot):
            payload = self.LD + multi._BATCH_MARKER + json.dumps(
                [str(path) for path in snapshot]
            )
            self.log(f"Dragging {len(snapshot)} selected Linux files")
            return multi._ORIGINAL_PROVIDER(self, payload)

    if isinstance(value, str) and value.startswith(self.CD):
        dragged_name = value[len(self.CD):].upper()
        snapshot = list(getattr(self, "_cpm_drag_snapshot", []))
        if len(snapshot) > 1 and any(
            item.name.upper() == dragged_name for item in snapshot
        ):
            payload = self.CD + multi._BATCH_MARKER + json.dumps(
                [item.name for item in snapshot]
            )
            self.log(f"Dragging {len(snapshot)} selected CP/M files")
            return multi._ORIGINAL_PROVIDER(self, payload)

    return multi._ORIGINAL_PROVIDER(self, value)


def _fixed_init(self, app):
    _ORIGINAL_MULTI_INIT(self, app)
    self._linux_drag_snapshot = []
    self._cpm_drag_snapshot = []
    _install_press_capture(self)


# Replace both the module globals used by launch_resizable's aggregate selection
# hooks and the class methods used by the ListBox row-selected signals.  The
# constructor wrapper installs capture-phase gestures after the panes exist.
multi._linux_selected = _linux_selected
multi._cpm_selected = _cpm_selected
multi._provider = _provider
multi.base.ui.Win.lselected = _linux_selected
multi.base.ui.Win.cselected = _cpm_selected
multi.base.ui.Win.provider = _provider
multi.base.ui.Win.__init__ = _fixed_init


if __name__ == "__main__":
    multi.base.ui.Adw.init()
    raise SystemExit(multi.base.ui.App().run(None))

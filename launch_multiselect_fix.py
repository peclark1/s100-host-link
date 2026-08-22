#!/usr/bin/env python3
"""Start Host Link with reliable multi-file drag-and-drop batching.

Gtk.ListBox in MULTIPLE mode may collapse a selection to the row under the
pointer as a drag starts.  By the time DragSource or DropTarget asks which rows
are selected, GTK can therefore report only the row where the drag began.

Avoid depending on GTK signal/gesture ordering.  A lightweight timer samples
the actual ListBox selections while they are stable.  When a drag collapses a
multi-selection, the drop handler can use the multi-selection that was observed
immediately before the collapse.
"""
from __future__ import annotations

import time
from pathlib import Path

import launch_resizable as multi

Gtk = multi.Gtk
GLib = multi.GLib

_ORIGINAL_MULTI_INIT = multi._resizable_init
_SELECTION_POLL_MS = 50
_RECENT_MULTI_SECONDS = 2.0


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


def _poll_selections(self):
    """Remember stable multi-selections before a drag can collapse them."""
    now = time.monotonic()

    linux = multi._linux_selected_paths(self)
    if len(linux) > 1:
        self._linux_recent_multi = list(linux)
        self._linux_recent_multi_time = now
        count = len(linux)
        if self._linux_last_logged_count != count:
            self.log(f"Linux selection observed: {count} files")
            self._linux_last_logged_count = count
    elif len(linux) == 0:
        self._linux_last_logged_count = 0

    cpm = multi._cpm_selected_files(self)
    if len(cpm) > 1:
        self._cpm_recent_multi = list(cpm)
        self._cpm_recent_multi_time = now
        count = len(cpm)
        if self._cpm_last_logged_count != count:
            self.log(f"CP/M selection observed: {count} files")
            self._cpm_last_logged_count = count
    elif len(cpm) == 0:
        self._cpm_last_logged_count = 0

    return True


def _provider(self, value):
    """Keep the normal one-file drag payload; batching is decided on drop."""
    return multi._ORIGINAL_PROVIDER(self, value)


def _linux_transfer_set(self, dragged: Path):
    """Choose the Linux files represented by this drag."""
    current = multi._linux_selected_paths(self)
    if len(current) > 1 and dragged in current:
        self.log(f"Drop found {len(current)} currently selected Linux files")
        return current

    previous = list(getattr(self, "_linux_recent_multi", []))
    age = time.monotonic() - getattr(self, "_linux_recent_multi_time", 0.0)
    if (
        len(previous) > 1
        and dragged in previous
        and age <= _RECENT_MULTI_SECONDS
    ):
        self.log(
            f"Using pre-drag Linux selection: {len(previous)} files "
            f"({age:.2f}s old)"
        )
        return previous

    return [dragged]


def _cpm_transfer_set(self, dragged_name: str):
    """Choose the CP/M files represented by this drag."""
    key = dragged_name.upper()

    current = multi._cpm_selected_files(self)
    if len(current) > 1 and any(item.name.upper() == key for item in current):
        self.log(f"Drop found {len(current)} currently selected CP/M files")
        return current

    previous = list(getattr(self, "_cpm_recent_multi", []))
    age = time.monotonic() - getattr(self, "_cpm_recent_multi_time", 0.0)
    if (
        len(previous) > 1
        and any(item.name.upper() == key for item in previous)
        and age <= _RECENT_MULTI_SECONDS
    ):
        self.log(
            f"Using pre-drag CP/M selection: {len(previous)} files "
            f"({age:.2f}s old)"
        )
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

    self.log(f"Drop: transferring {len(files)} CP/M file(s) to Linux")
    return multi._start_receive_batch(self, files)


def _fixed_init(self, app):
    self._linux_recent_multi = []
    self._cpm_recent_multi = []
    self._linux_recent_multi_time = 0.0
    self._cpm_recent_multi_time = 0.0
    self._linux_last_logged_count = 0
    self._cpm_last_logged_count = 0

    _ORIGINAL_MULTI_INIT(self, app)
    self._selection_poll_source = GLib.timeout_add(
        _SELECTION_POLL_MS, lambda: _poll_selections(self)
    )
    self.log("Multi-file drag support active: polled selection batching v5")


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

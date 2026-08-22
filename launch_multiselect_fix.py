#!/usr/bin/env python3
"""Start Host Link with reliable multi-file drag-and-drop batching.

On the target GTK/PyGObject build, Gtk.ListBox visibly paints multiple rows as
selected but get_selected_rows() can yield only the row involved in the current
pointer interaction.  Read selection state from each Gtk.ListBoxRow directly
instead.  The drop handler then resolves the dragged row back to the complete
visible selection and starts the existing batch-transfer worker.
"""
from __future__ import annotations

from pathlib import Path

import launch_resizable as multi

Gtk = multi.Gtk

_ORIGINAL_MULTI_INIT = multi._resizable_init


def _selected_rows(listbox):
    """Return rows whose own GTK selected state is true, in display order."""
    rows = []
    index = 0
    while True:
        row = listbox.get_row_at_index(index)
        if row is None:
            break
        try:
            selected = bool(row.is_selected())
        except (AttributeError, TypeError):
            # Compatibility fallback for older bindings.  This is not expected
            # on GTK4, but keeps the test branch usable if is_selected() is not
            # exposed by a particular PyGObject package.
            selected = any(
                candidate.get_index() == index
                for candidate in listbox.get_selected_rows()
            )
        if selected:
            rows.append(row)
        index += 1
    return rows


def _linux_selected_paths(self):
    """Return all visibly selected Linux files in row order."""
    paths = []
    for row in _selected_rows(self.ll):
        path = multi._linux_path_for_row(self, row)
        if path is not None and path.is_file():
            paths.append(path)
    return paths


def _cpm_selected_files(self):
    """Return all visibly selected CP/M files in row order."""
    files = []
    for row in _selected_rows(self.cl):
        index = row.get_index()
        if 0 <= index < len(self.cfiles):
            files.append(self.cfiles[index])
    return files


def _linux_selected(self, _listbox, _row=None):
    paths = _linux_selected_paths(self)
    self.lsels = paths
    self.lsel = paths[0] if paths else None
    count = len(paths)
    if count != getattr(self, "_linux_logged_selection_count", -1):
        if count > 1:
            self.log(f"Linux visible selection: {count} files")
        self._linux_logged_selection_count = count
    self.buttons()


def _cpm_selected(self, _listbox, _row=None):
    files = _cpm_selected_files(self)
    self.csels = files
    self.csel = files[0] if files else None
    count = len(files)
    if count != getattr(self, "_cpm_logged_selection_count", -1):
        if count > 1:
            self.log(f"CP/M visible selection: {count} files")
        self._cpm_logged_selection_count = count
    self.buttons()


def _provider(self, value):
    """Keep the normal one-file drag payload; batching is resolved on drop."""
    return multi._ORIGINAL_PROVIDER(self, value)


def _linux_transfer_set(self, dragged: Path):
    """Resolve a Linux drag to all visibly selected files when appropriate."""
    selected = _linux_selected_paths(self)
    if len(selected) > 1 and dragged in selected:
        self.log(f"Drop found {len(selected)} visibly selected Linux files")
        return selected

    # Gtk may clear/collapse the selection during the drag itself.  Keep the
    # most recent true multi-selection recorded by the selection callback.
    previous = list(getattr(self, "_linux_last_multi", []))
    if len(previous) > 1 and dragged in previous:
        self.log(f"Using remembered Linux selection: {len(previous)} files")
        return previous

    return [dragged]


def _cpm_transfer_set(self, dragged_name: str):
    """Resolve a CP/M drag to all visibly selected files when appropriate."""
    key = dragged_name.upper()
    selected = _cpm_selected_files(self)
    if len(selected) > 1 and any(item.name.upper() == key for item in selected):
        self.log(f"Drop found {len(selected)} visibly selected CP/M files")
        return selected

    previous = list(getattr(self, "_cpm_last_multi", []))
    if len(previous) > 1 and any(item.name.upper() == key for item in previous):
        self.log(f"Using remembered CP/M selection: {len(previous)} files")
        return previous

    return [item for item in self.cfiles if item.name.upper() == key][:1]


def _remember_linux_selection(self):
    paths = _linux_selected_paths(self)
    if len(paths) > 1:
        self._linux_last_multi = list(paths)
        count = len(paths)
        if count != getattr(self, "_linux_logged_selection_count", -1):
            self.log(f"Linux visible selection: {count} files")
            self._linux_logged_selection_count = count
    elif not paths:
        self._linux_last_multi = []
    return True


def _remember_cpm_selection(self):
    files = _cpm_selected_files(self)
    if len(files) > 1:
        self._cpm_last_multi = list(files)
        count = len(files)
        if count != getattr(self, "_cpm_logged_selection_count", -1):
            self.log(f"CP/M visible selection: {count} files")
            self._cpm_logged_selection_count = count
    elif not files:
        self._cpm_last_multi = []
    return True


def _poll_visible_selection(self):
    """Remember direct row state before a drag is able to collapse it."""
    _remember_linux_selection(self)
    _remember_cpm_selection(self)
    return True


def _drop_on_cpm(self, _target, value, _x, _y):
    """Drop Linux file(s) onto CP/M, resolving batching at destination."""
    batch = multi._decode_batch(value, self.LD)
    if batch is not None:
        paths = [Path(item) for item in batch]
    else:
        if not isinstance(value, str) or not value.startswith(self.LD):
            return False
        dragged = Path(value[len(self.LD):])
        paths = _linux_transfer_set(self, dragged)

    self.log(f"Drop: transferring {len(paths)} Linux file(s) to CP/M")
    result = multi._start_send_batch(self, paths)
    self._linux_last_multi = []
    return result


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
    result = multi._start_receive_batch(self, files)
    self._cpm_last_multi = []
    return result


def _fixed_init(self, app):
    self._linux_last_multi = []
    self._cpm_last_multi = []
    self._linux_logged_selection_count = -1
    self._cpm_logged_selection_count = -1

    _ORIGINAL_MULTI_INIT(self, app)

    # Sample direct row state frequently enough that an ordinary human drag
    # cannot erase the preceding multi-selection before it has been remembered.
    self._selection_poll_source = multi.GLib.timeout_add(
        40, lambda: _poll_visible_selection(self)
    )
    self.log("Multi-file drag support active: direct-row selection v6")


# Replace the helper functions that launch_resizable's selection callbacks use,
# plus the drop handlers that decide whether a drag is a single or batch copy.
multi._linux_selected_paths = _linux_selected_paths
multi._cpm_selected_files = _cpm_selected_files
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

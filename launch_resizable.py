#!/usr/bin/env python3
"""Launch the dual-pane Host Link UI with a draggable Linux/CP/M divider.

This builds on launch_dualpane.py, which installs the GVFS/SMB-safe GET path,
and layers UI refinements over the development dual-pane window:

* native draggable Gtk.Paned divider
* reliable Linux/CP/M row selection using ListBox row indexes
* Ctrl/Shift multi-selection on both Linux and CP/M file lists
* multi-file drag-and-drop transfers in either direction
* automatic, debounced CP/M directory refresh after drive/user changes
* manual refresh buttons for the Linux directory and serial-port list
* drag-and-drop-only file transfer controls; Send/Receive buttons are hidden
* startup settings restore without overwriting the saved baud rate
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import launch_dualpane as base

Adw = base.ui.Adw
Gtk = base.ui.Gtk
GLib = base.ui.GLib

_ORIGINAL_INIT = base.ui.Win.__init__
_ORIGINAL_TARGET = base.ui.Win.target
_ORIGINAL_SAVE = base.ui.Win.save
_ORIGINAL_PROVIDER = base.ui.Win.provider
_ORIGINAL_DONE = base.ui.Win.done
_ORIGINAL_ERR = base.ui.Win.err

_BATCH_MARKER = "BATCH:"


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _find_ancestor(widget, widget_type):
    """Return the first parent/ancestor matching widget_type."""
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, widget_type):
            return parent
        parent = parent.get_parent()
    return None


def _find_two_frame_box(widget):
    """Find the horizontal box that currently holds Linux and CP/M frames."""
    for child in _children(widget):
        if isinstance(child, Gtk.Box) and child.get_orientation() == Gtk.Orientation.HORIZONTAL:
            kids = list(_children(child))
            if len(kids) == 2 and all(isinstance(item, Gtk.Frame) for item in kids):
                return child, kids[0], kids[1]
        found = _find_two_frame_box(child)
        if found is not None:
            return found
    return None


def _linux_path_for_row(self, row):
    """Return the Linux Path represented by a ListBox row.

    Do not use Python id(row) here. PyGObject may hand a signal callback a
    different Python wrapper for the same underlying GTK object, which left the
    visible row selected while Send stayed disabled. Dict insertion order
    matches the row insertion order, so the GTK row index is stable here.
    """
    if row is None:
        return None
    index = row.get_index()
    entries = list(self.lrows.values())
    if 0 <= index < len(entries):
        return entries[index]
    return None


def _linux_selected_paths(self):
    """Return all selected Linux files in visible row order."""
    rows = sorted(self.ll.get_selected_rows(), key=lambda row: row.get_index())
    paths = []
    for row in rows:
        path = _linux_path_for_row(self, row)
        if path is not None and path.is_file():
            paths.append(path)
    return paths


def _cpm_selected_files(self):
    """Return all selected CP/M files in visible row order."""
    rows = sorted(self.cl.get_selected_rows(), key=lambda row: row.get_index())
    files = []
    for row in rows:
        index = row.get_index()
        if 0 <= index < len(self.cfiles):
            files.append(self.cfiles[index])
    return files


def _linux_selected(self, _listbox, _row=None):
    paths = _linux_selected_paths(self)
    self.lsels = paths
    self.lsel = paths[0] if paths else None
    self.buttons()


def _linux_activated(self, _listbox, row):
    path = _linux_path_for_row(self, row)
    if path is not None and path.is_dir():
        self.setdir(path)
    elif path is not None and path.is_file():
        # Activation should not collapse a Ctrl/Shift multi-selection.
        paths = _linux_selected_paths(self)
        self.lsels = paths
        self.lsel = paths[0] if paths else path
        self.buttons()


def _cpm_selected(self, _listbox, _row=None):
    files = _cpm_selected_files(self)
    self.csels = files
    self.csel = files[0] if files else None
    self.buttons()


def _save_settings(self, *args):
    """Avoid clobbering saved settings while startup widgets are initialized."""
    if getattr(self, "_suppress_settings_save", False):
        return
    return _ORIGINAL_SAVE(self, *args)


def _target_changed(self, *args):
    """Save drive/user and automatically refresh the selected CP/M directory."""
    _ORIGINAL_TARGET(self, *args)

    # restore() sets drive then user during application startup. Preserve that
    # behavior without issuing one or two surprise serial requests on launch.
    if getattr(self, "_suppress_target_refresh", False):
        return

    previous = getattr(self, "_target_refresh_source", 0)
    if previous:
        try:
            GLib.source_remove(previous)
        except Exception:
            pass

    # A short debounce combines a quick drive+user change into one HOST request.
    def refresh_selected_target():
        self._target_refresh_source = 0
        if not self.busy and self.port():
            self.crefresh()
        return False

    self._target_refresh_source = GLib.timeout_add(250, refresh_selected_target)


def _provider(self, value):
    """Encode the full current selection when a selected row is dragged."""
    if isinstance(value, str) and value.startswith(self.LD):
        dragged = Path(value[len(self.LD):])
        paths = _linux_selected_paths(self)
        if len(paths) > 1 and dragged in paths:
            payload = self.LD + _BATCH_MARKER + json.dumps([str(path) for path in paths])
            return _ORIGINAL_PROVIDER(self, payload)

    if isinstance(value, str) and value.startswith(self.CD):
        dragged_name = value[len(self.CD):]
        files = _cpm_selected_files(self)
        if len(files) > 1 and any(item.name == dragged_name for item in files):
            payload = self.CD + _BATCH_MARKER + json.dumps([item.name for item in files])
            return _ORIGINAL_PROVIDER(self, payload)

    return _ORIGINAL_PROVIDER(self, value)


def _decode_batch(value, prefix):
    marker = prefix + _BATCH_MARKER
    if not isinstance(value, str) or not value.startswith(marker):
        return None
    try:
        decoded = json.loads(value[len(marker):])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if isinstance(item, str) and item]


def _duplicate_cpm_names(paths):
    names = {}
    duplicates = []
    for path in paths:
        remote = base.ui.cpm_filename(str(path))
        key = remote.upper()
        if key in names and key not in duplicates:
            duplicates.append(key)
        names[key] = path
    return duplicates


def _start_send_batch(self, paths):
    if self.busy or not self.port():
        return False

    clean = []
    seen = set()
    for path in paths:
        path = Path(path)
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if path.is_file() and key not in seen:
            clean.append(path)
            seen.add(key)

    if not clean:
        return False

    duplicates = _duplicate_cpm_names(clean)
    if duplicates:
        self.toast(
            "Selected Linux files map to the same CP/M 8.3 filename: "
            + ", ".join(duplicates)
        )
        return False

    count = len(clean)
    self._batch_refresh_cpm_on_error = True
    self._batch_refresh_linux_on_error = False
    label = f"Sending {clean[0].name}…" if count == 1 else f"Sending {count} files…"
    self.begin(label)
    self.worker = threading.Thread(
        target=_send_batch_worker,
        args=(self, clean),
        daemon=True,
    )
    self.worker.start()
    return True


def _send_batch_worker(self, paths):
    total_bytes = 0
    total_blocks = 0
    total_retries = 0
    completed = 0
    count = len(paths)

    try:
        with self.ser() as ser:
            link = self.link(ser)
            for index, path in enumerate(paths, start=1):
                self._batch_progress_prefix = (
                    f"Sending {path.name}"
                    if count == 1
                    else f"Sending {index}/{count}: {path.name}"
                )
                stats = link.send_file(
                    str(path),
                    base.ui.cpm_filename(str(path)),
                    self.drive(),
                    self.user(),
                )
                total_bytes += stats.bytes_in_file
                total_blocks += stats.blocks_sent
                total_retries += stats.retries
                completed += 1

        aggregate = base.ui.TransferStats(
            bytes_in_file=total_bytes,
            blocks_sent=total_blocks,
            retries=total_retries,
            mode="HOST2/BATCH" if count > 1 else "HOST2/CRC",
        )
        GLib.idle_add(_send_batch_done, self, aggregate, count)
    except Exception as exc:
        message = str(exc)
        if count > 1:
            message = f"Batch stopped after {completed}/{count} file(s): {message}"
        GLib.idle_add(_batch_error, self, message)


def _send_batch_done(self, stats, count):
    self._batch_progress_prefix = ""
    self._batch_refresh_cpm_on_error = False
    self._batch_refresh_linux_on_error = False
    verb = "Sent" if count == 1 else f"Sent {count} files"
    return _ORIGINAL_DONE(self, stats, verb)


def _unique_destination(directory, name, reserved):
    destination = directory / name
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 1
    while destination.exists() or destination in reserved:
        destination = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    reserved.add(destination)
    return destination


def _start_receive_batch(self, files):
    if self.busy or not self.port():
        return False

    clean = []
    seen = set()
    for item in files:
        key = item.name.upper()
        if key not in seen:
            clean.append(item)
            seen.add(key)

    if not clean:
        return False

    reserved = set()
    destinations = [
        (item, _unique_destination(self.ldir, item.name, reserved))
        for item in clean
    ]

    count = len(destinations)
    self._batch_refresh_cpm_on_error = False
    self._batch_refresh_linux_on_error = True
    label = (
        f"Receiving {destinations[0][0].name}…"
        if count == 1
        else f"Receiving {count} files…"
    )
    self.begin(label)
    self.worker = threading.Thread(
        target=_receive_batch_worker,
        args=(self, destinations),
        daemon=True,
    )
    self.worker.start()
    return True


def _receive_batch_worker(self, transfers):
    total_bytes = 0
    total_blocks = 0
    total_retries = 0
    completed = 0
    count = len(transfers)

    try:
        with self.ser() as ser:
            link = self.link(ser)
            for index, (item, destination) in enumerate(transfers, start=1):
                self._batch_progress_prefix = (
                    f"Receiving {item.name}"
                    if count == 1
                    else f"Receiving {index}/{count}: {item.name}"
                )
                stats = link.receive_file(
                    item.name,
                    str(destination),
                    self.drive(),
                    self.user(),
                    item.size_bytes,
                )
                total_bytes += stats.bytes_in_file
                total_blocks += stats.blocks_sent
                total_retries += stats.retries
                completed += 1

        aggregate = base.ui.TransferStats(
            bytes_in_file=total_bytes,
            blocks_sent=total_blocks,
            retries=total_retries,
            mode="HOST2/GET-BATCH" if count > 1 else "HOST2/GET",
        )
        GLib.idle_add(_receive_batch_done, self, aggregate, count)
    except Exception as exc:
        message = str(exc)
        if count > 1:
            message = f"Batch stopped after {completed}/{count} file(s): {message}"
        GLib.idle_add(_batch_error, self, message)


def _receive_batch_done(self, stats, count):
    self._batch_progress_prefix = ""
    self._batch_refresh_cpm_on_error = False
    self._batch_refresh_linux_on_error = False
    self.busy = False
    self.prog.set_fraction(1)
    if count == 1:
        self.status.set_text("Receive complete")
        self.log(f"Received {stats.bytes_in_file:,} bytes")
    else:
        self.status.set_text(f"Received {count} files")
        self.log(
            f"Received {count} files: {stats.bytes_in_file:,} bytes, "
            f"{stats.blocks_sent} CP/M record(s)"
        )
    self.lrefresh()
    self.buttons()
    return False


def _batch_error(self, message):
    self._batch_progress_prefix = ""
    refresh_linux = getattr(self, "_batch_refresh_linux_on_error", False)
    refresh_cpm = getattr(self, "_batch_refresh_cpm_on_error", False)
    self._batch_refresh_cpm_on_error = False
    self._batch_refresh_linux_on_error = False
    result = _ORIGINAL_ERR(self, message)
    if refresh_linux:
        self.lrefresh()
    if refresh_cpm:
        GLib.timeout_add(350, self.after)
    return result


def _progress(self, done, total, stats):
    fraction = 1 if not total else min(1, done / total)
    self.prog.set_fraction(fraction)
    prefix = getattr(self, "_batch_progress_prefix", "")
    detail = f"{fraction * 100:.1f}% — block {stats.blocks_sent}"
    self.status.set_text(f"{prefix} — {detail}" if prefix else detail)
    return False


def _send_selected(self, *_args):
    return _start_send_batch(self, _linux_selected_paths(self))


def _receive_selected(self, *_args):
    return _start_receive_batch(self, _cpm_selected_files(self))


def _drop_on_cpm(self, _target, value, _x, _y):
    batch = _decode_batch(value, self.LD)
    if batch is not None:
        return _start_send_batch(self, [Path(item) for item in batch])

    if not isinstance(value, str) or not value.startswith(self.LD):
        return False
    path = Path(value[len(self.LD):])
    return _start_send_batch(self, [path])


def _drop_on_linux(self, _target, value, _x, _y):
    batch = _decode_batch(value, self.CD)
    if batch is not None:
        wanted = {name.upper() for name in batch}
        files = [item for item in self.cfiles if item.name.upper() in wanted]
        # Preserve the order encoded by the drag source.
        by_name = {item.name.upper(): item for item in files}
        ordered = [by_name[name.upper()] for name in batch if name.upper() in by_name]
        return _start_receive_batch(self, ordered)

    if not isinstance(value, str) or not value.startswith(self.CD):
        return False
    name = value[len(self.CD):]
    item = next((entry for entry in self.cfiles if entry.name == name), None)
    return _start_receive_batch(self, [item] if item is not None else [])


def _install_usability_controls(self):
    """Add manual refresh controls and make drag-and-drop the transfer UI."""
    # Adw.ActionRow inserts suffix widgets into an internal container, so the
    # dropdown's immediate GTK parent is not the ActionRow itself. Walk upward
    # to find the owning row, then add the serial refresh button beside it.
    port_row = _find_ancestor(self.pdd, Adw.ActionRow)
    if port_row is not None:
        self.port_refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.port_refresh_button.set_tooltip_text("Refresh serial ports")
        self.port_refresh_button.set_valign(Gtk.Align.CENTER)
        self.port_refresh_button.connect("clicked", lambda _button: self.refresh_ports())
        port_row.add_suffix(self.port_refresh_button)
    else:
        self.log("WARNING: Could not locate USB-device row for serial refresh button.")

    linux_toolbar = self.path.get_parent()
    if isinstance(linux_toolbar, Gtk.Box):
        self.linux_refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.linux_refresh_button.set_tooltip_text("Refresh Linux file list")
        self.linux_refresh_button.connect("clicked", lambda _button: self.lrefresh())
        linux_toolbar.append(self.linux_refresh_button)

    # Drag-and-drop already calls send()/recv(). Keep those methods and the
    # hidden button objects intact because Win.buttons() still updates their
    # sensitivity; simply remove the visible Send/Receive controls.
    actions = self.sb.get_parent() if hasattr(self, "sb") else None
    if actions is not None:
        actions.remove(self.sb)
        if hasattr(self, "rb") and self.rb.get_parent() is actions:
            actions.remove(self.rb)


def _install_multi_selection(self):
    """Enable normal Ctrl/Shift multi-selection in both file panes."""
    self.ll.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
    self.cl.set_selection_mode(Gtk.SelectionMode.MULTIPLE)

    self.ll.set_tooltip_text(
        "Ctrl-click or Shift-click to select multiple Linux files; "
        "drag any selected file to CP/M to copy the group."
    )
    self.cl.set_tooltip_text(
        "Ctrl-click or Shift-click to select multiple CP/M files; "
        "drag any selected file to Linux to copy the group."
    )

    # MULTIPLE selection has a dedicated aggregate-change signal. Keep the
    # existing row-selected handlers for compatibility, but always recompute
    # selection from ListBox.get_selected_rows().
    self.ll.connect(
        "selected-rows-changed",
        lambda listbox: _linux_selected(self, listbox),
    )
    self.cl.connect(
        "selected-rows-changed",
        lambda listbox: _cpm_selected(self, listbox),
    )

    self.lsels = _linux_selected_paths(self)
    self.csels = _cpm_selected_files(self)
    self.lsel = self.lsels[0] if self.lsels else None
    self.csel = self.csels[0] if self.csels else None
    self.buttons()


def _resizable_init(self, app):
    # The base constructor does ui() -> refresh_ports() -> restore(). Selecting
    # a serial port during refresh_ports() emits notify::selected and calls
    # save(), while the baud widget is still at its default 9600. Suppress all
    # settings writes until restore() has finished so a saved 115200 (or any
    # other baud) is not overwritten during startup.
    self._suppress_settings_save = True
    self._suppress_target_refresh = True
    self._target_refresh_source = 0
    self._batch_progress_prefix = ""
    self._batch_refresh_cpm_on_error = False
    self._batch_refresh_linux_on_error = False
    _ORIGINAL_INIT(self, app)
    self._suppress_settings_save = False
    self._suppress_target_refresh = False

    _install_usability_controls(self)
    _install_multi_selection(self)

    found = _find_two_frame_box(self)
    if found is None:
        self.log("WARNING: Could not locate Linux/CP/M pane container; using fixed layout.")
        return

    holder, linux_frame, cpm_frame = found
    holder.remove(linux_frame)
    holder.remove(cpm_frame)

    paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
    paned.set_hexpand(True)
    paned.set_vexpand(True)
    paned.set_resize_start_child(True)
    paned.set_resize_end_child(True)
    paned.set_shrink_start_child(False)
    paned.set_shrink_end_child(False)

    # Keep either side useful even if the divider is dragged aggressively.
    linux_frame.set_size_request(320, -1)
    cpm_frame.set_size_request(320, -1)
    paned.set_start_child(linux_frame)
    paned.set_end_child(cpm_frame)
    holder.append(paned)

    self.pane_divider = paned

    # Gtk.Paned needs an allocation before a true 50/50 position is meaningful.
    def center_divider():
        width = paned.get_allocated_width()
        if width > 0:
            paned.set_position(width // 2)
            return False
        return True

    GLib.idle_add(center_divider)


# Install behavior before the first Win instance is constructed, so the signal
# connections created by Win.ui() bind to these corrected callbacks.
base.ui.Win.lselected = _linux_selected
base.ui.Win.lactivate = _linux_activated
base.ui.Win.cselected = _cpm_selected
base.ui.Win.save = _save_settings
base.ui.Win.target = _target_changed
base.ui.Win.provider = _provider
base.ui.Win.send = _send_selected
base.ui.Win.recv = _receive_selected
base.ui.Win.dropc = _drop_on_cpm
base.ui.Win.dropl = _drop_on_linux
base.ui.Win.pg = _progress
base.ui.Win.err = _batch_error
base.ui.Win.__init__ = _resizable_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

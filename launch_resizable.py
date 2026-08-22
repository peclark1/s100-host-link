#!/usr/bin/env python3
"""Shared production UI support for the S-100 Host Link desktop application.

This layer builds on launch_dualpane.py and provides the common behavior used by
the model-backed file panes in launch_listview.py:

* draggable Linux/CP/M pane divider
* sequential multi-file transfers in either direction
* automatic CP/M directory refresh after drive/user changes
* manual refresh controls for Linux files and serial ports
* drag-and-drop-only transfer controls; Send/Receive buttons stay hidden
* startup settings restore without overwriting the saved baud rate
"""
from __future__ import annotations

import threading
from pathlib import Path

import launch_dualpane as base

Adw = base.ui.Adw
Gtk = base.ui.Gtk
GLib = base.ui.GLib

_ORIGINAL_INIT = base.ui.Win.__init__
_ORIGINAL_TARGET = base.ui.Win.target
_ORIGINAL_SAVE = base.ui.Win.save
_ORIGINAL_DONE = base.ui.Win.done
_ORIGINAL_ERR = base.ui.Win.err


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _find_ancestor(widget, widget_type):
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, widget_type):
            return parent
        parent = parent.get_parent()
    return None


def _find_two_frame_box(widget):
    for child in _children(widget):
        if isinstance(child, Gtk.Box) and child.get_orientation() == Gtk.Orientation.HORIZONTAL:
            kids = list(_children(child))
            if len(kids) == 2 and all(isinstance(item, Gtk.Frame) for item in kids):
                return child, kids[0], kids[1]
        found = _find_two_frame_box(child)
        if found is not None:
            return found
    return None


def _save_settings(self, *args):
    """Avoid clobbering saved settings while startup widgets are initialized."""
    if getattr(self, "_suppress_settings_save", False):
        return
    return _ORIGINAL_SAVE(self, *args)


def _target_changed(self, *args):
    """Save drive/user and automatically refresh the selected CP/M directory."""
    _ORIGINAL_TARGET(self, *args)

    if getattr(self, "_suppress_target_refresh", False):
        return

    previous = getattr(self, "_target_refresh_source", 0)
    if previous:
        try:
            GLib.source_remove(previous)
        except Exception:
            pass

    def refresh_selected_target():
        self._target_refresh_source = 0
        if not self.busy and self.port():
            self.crefresh()
        return False

    self._target_refresh_source = GLib.timeout_add(250, refresh_selected_target)


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
    """Send one or more Linux files sequentially over a single serial session."""
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
    """Receive one or more CP/M files sequentially over a single serial session."""
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


def _install_usability_controls(self):
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

    actions = self.sb.get_parent() if hasattr(self, "sb") else None
    if actions is not None:
        actions.remove(self.sb)
        if hasattr(self, "rb") and self.rb.get_parent() is actions:
            actions.remove(self.rb)


def _resizable_init(self, app):
    """Initialize common desktop behavior before file panes are replaced."""
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

    linux_frame.set_size_request(320, -1)
    cpm_frame.set_size_request(320, -1)
    paned.set_start_child(linux_frame)
    paned.set_end_child(cpm_frame)
    holder.append(paned)
    self.pane_divider = paned

    def center_divider():
        width = paned.get_allocated_width()
        if width > 0:
            paned.set_position(width // 2)
            return False
        return True

    GLib.idle_add(center_divider)


base.ui.Win.save = _save_settings
base.ui.Win.target = _target_changed
base.ui.Win.pg = _progress
base.ui.Win.err = _batch_error
base.ui.Win.__init__ = _resizable_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

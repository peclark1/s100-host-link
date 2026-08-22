#!/usr/bin/env python3
"""Launch the production dual-pane S-100 Host Link UI.

This layer builds on launch_dualpane.py and provides the current desktop UI:

* draggable Linux/CP/M pane divider
* stable Linux row mapping for PyGObject
* explicit per-file checkboxes for multi-file transfers
* sequential batch transfers in either direction
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
_ORIGINAL_LREFRESH = base.ui.Win.lrefresh
_ORIGINAL_CRENDER = base.ui.Win.crender


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


def _scan_linux_entries(self):
    """Return directory entries in the same order used by the Linux pane."""
    try:
        return sorted(
            self.ldir.iterdir(),
            key=lambda path: (not path.is_dir(), path.name.casefold()),
        )
    except OSError:
        return []


def _rebuild_linux_row_map(self):
    """Use stable row indexes instead of Python id(Gtk.ListBoxRow) keys."""
    entries = _scan_linux_entries(self)
    self.lrows = {index: path for index, path in enumerate(entries)}
    return entries


def _linux_path_for_row(self, row):
    if row is None:
        return None
    index = row.get_index()
    if index in self.lrows:
        return self.lrows[index]
    entries = list(self.lrows.values())
    if 0 <= index < len(entries):
        return entries[index]
    return None


def _linux_selected(self, _listbox, row):
    path = _linux_path_for_row(self, row)
    self.lsel = path if path is not None and path.is_file() else None
    self.buttons()


def _linux_activated(self, _listbox, row):
    path = _linux_path_for_row(self, row)
    if path is not None and path.is_dir():
        self.setdir(path)
    elif path is not None and path.is_file():
        self.lsel = path
        self.buttons()


def _cpm_selected(self, _listbox, row):
    if row is None:
        self.csel = None
    else:
        index = row.get_index()
        self.csel = self.cfiles[index] if 0 <= index < len(self.cfiles) else None
    self.buttons()


def _save_settings(self, *args):
    if getattr(self, "_suppress_settings_save", False):
        return
    return _ORIGINAL_SAVE(self, *args)


def _target_changed(self, *args):
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


def _linux_key(path: Path) -> str:
    return str(Path(path))


def _cpm_key(name: str) -> str:
    return str(name).upper()


def _linux_checked_paths(self):
    wanted = set(getattr(self, "_linux_checked", set()))
    result = []
    for path in self.lrows.values():
        path = Path(path)
        if path.is_file() and _linux_key(path) in wanted:
            result.append(path)
    return result


def _cpm_checked_files(self):
    wanted = set(getattr(self, "_cpm_checked", set()))
    return [item for item in self.cfiles if _cpm_key(item.name) in wanted]


def _set_linux_checked(self, button, path: Path):
    key = _linux_key(path)
    if button.get_active():
        self._linux_checked.add(key)
    else:
        self._linux_checked.discard(key)
    if getattr(self, "_suppress_check_feedback", False):
        return
    count = len(self._linux_checked)
    self.status.set_text(
        "Ready" if count == 0 else f"{count} Linux file(s) selected for copy"
    )


def _set_cpm_checked(self, button, item):
    key = _cpm_key(item.name)
    if button.get_active():
        self._cpm_checked.add(key)
    else:
        self._cpm_checked.discard(key)
    if getattr(self, "_suppress_check_feedback", False):
        return
    count = len(self._cpm_checked)
    self.status.set_text(
        "Ready" if count == 0 else f"{count} CP/M file(s) selected for copy"
    )


def _prepend_checkbox(row, tooltip):
    box = row.get_child()
    if not isinstance(box, Gtk.Box):
        return None
    button = Gtk.CheckButton()
    button.set_valign(Gtk.Align.CENTER)
    button.set_tooltip_text(tooltip)
    box.prepend(button)
    return button


def _linux_refresh(self):
    self._linux_checked = set()
    self._linux_checkbuttons = {}
    result = _ORIGINAL_LREFRESH(self)

    entries = _rebuild_linux_row_map(self)
    for index, path in enumerate(entries):
        row = self.ll.get_row_at_index(index)
        if row is None:
            continue
        path = Path(path)
        if not path.is_file():
            continue
        button = _prepend_checkbox(
            row,
            "Include this file in a multi-file transfer",
        )
        if button is None:
            continue
        key = _linux_key(path)
        self._linux_checkbuttons[key] = button
        button.connect("toggled", lambda b, p=path: _set_linux_checked(self, b, p))

    return result


def _cpm_render(self, files):
    self._cpm_checked = set()
    self._cpm_checkbuttons = {}
    result = _ORIGINAL_CRENDER(self, files)

    for index, item in enumerate(self.cfiles):
        row = self.cl.get_row_at_index(index)
        if row is None:
            continue
        button = _prepend_checkbox(
            row,
            "Include this file in a multi-file transfer",
        )
        if button is None:
            continue
        key = _cpm_key(item.name)
        self._cpm_checkbuttons[key] = button
        button.connect("toggled", lambda b, f=item: _set_cpm_checked(self, b, f))

    return result


def _clear_linux_checks(self):
    self._suppress_check_feedback = True
    try:
        for button in getattr(self, "_linux_checkbuttons", {}).values():
            if button.get_active():
                button.set_active(False)
        self._linux_checked.clear()
    finally:
        self._suppress_check_feedback = False


def _clear_cpm_checks(self):
    self._suppress_check_feedback = True
    try:
        for button in getattr(self, "_cpm_checkbuttons", {}).values():
            if button.get_active():
                button.set_active(False)
        self._cpm_checked.clear()
    finally:
        self._suppress_check_feedback = False


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


def _drop_on_cpm(self, _target, value, _x, _y):
    if not isinstance(value, str) or not value.startswith(self.LD):
        return False

    dragged = Path(value[len(self.LD):])
    checked = _linux_checked_paths(self)
    dragged_is_checked = _linux_key(dragged) in self._linux_checked
    paths = checked if dragged_is_checked and checked else [dragged]

    started = _start_send_batch(self, paths)
    if started and dragged_is_checked:
        _clear_linux_checks(self)
    return started


def _drop_on_linux(self, _target, value, _x, _y):
    if not isinstance(value, str) or not value.startswith(self.CD):
        return False

    dragged_name = value[len(self.CD):]
    checked = _cpm_checked_files(self)
    dragged_is_checked = _cpm_key(dragged_name) in self._cpm_checked

    if dragged_is_checked and checked:
        files = checked
    else:
        item = next(
            (entry for entry in self.cfiles if entry.name.upper() == dragged_name.upper()),
            None,
        )
        files = [item] if item is not None else []

    started = _start_receive_batch(self, files)
    if started and dragged_is_checked:
        _clear_cpm_checks(self)
    return started


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
    self._suppress_settings_save = True
    self._suppress_target_refresh = True
    self._target_refresh_source = 0
    self._batch_progress_prefix = ""
    self._batch_refresh_cpm_on_error = False
    self._batch_refresh_linux_on_error = False
    self._linux_checked = set()
    self._cpm_checked = set()
    self._linux_checkbuttons = {}
    self._cpm_checkbuttons = {}
    self._suppress_check_feedback = False

    _ORIGINAL_INIT(self, app)

    self._suppress_settings_save = False
    self._suppress_target_refresh = False

    _install_usability_controls(self)

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


base.ui.Win.lselected = _linux_selected
base.ui.Win.lactivate = _linux_activated
base.ui.Win.cselected = _cpm_selected
base.ui.Win.save = _save_settings
base.ui.Win.target = _target_changed
base.ui.Win.lrefresh = _linux_refresh
base.ui.Win.crender = _cpm_render
base.ui.Win.dropc = _drop_on_cpm
base.ui.Win.dropl = _drop_on_linux
base.ui.Win.pg = _progress
base.ui.Win.err = _batch_error
base.ui.Win.__init__ = _resizable_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

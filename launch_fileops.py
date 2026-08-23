#!/usr/bin/env python3
"""CP/M file-management layer for the production Host Link UI.

Adds native CP/M rename, delete, and file-attribute operations while preserving
Gtk.ListView multi-selection and drag-and-drop transfers.
"""
from __future__ import annotations

import re
import threading

import launch_listview as current
import s100_hostlink_gtk4 as protocol

base = current.base
Gtk = current.Gtk
Gdk = current.Gdk
GLib = base.ui.GLib
Adw = base.ui.Adw
HostLinkV2 = base.ui.HostLinkV2
TransferStats = base.ui.TransferStats
XModemError = base.ui.XModemError

CMD_DELETE = 4
CMD_RENAME = 5
CMD_ATTR = 6
ATTR_RO = 0x01
ATTR_SYS = 0x02
ATTR_ARC = 0x04

HostLinkV2.CMD_DELETE = CMD_DELETE
HostLinkV2.CMD_RENAME = CMD_RENAME
HostLinkV2.CMD_ATTR = CMD_ATTR

_CPM_NAME_RE = re.compile(r"^[A-Z0-9_$#@!%&'()\-{}^~]{1,8}(?:\.[A-Z0-9_$#@!%&'()\-{}^~]{1,3})?$")
_ORIGINAL_SETUP_ROW = current._setup_row
_ORIGINAL_INIT = base.ui.Win.__init__


def _normalize_cpm_name(value: str) -> str:
    name = value.strip().upper()
    if not _CPM_NAME_RE.fullmatch(name):
        raise ValueError(
            "Use a CP/M 8.3 filename: 1-8 name characters and an optional "
            "1-3 character extension; wildcards are not allowed."
        )
    return name


def _attribute_bits(attributes: str) -> int:
    parts = {part.strip().upper() for part in (attributes or "").split(",") if part.strip()}
    bits = 0
    if "R/O" in parts:
        bits |= ATTR_RO
    if "SYS" in parts:
        bits |= ATTR_SYS
    if "ARC" in parts:
        bits |= ATTR_ARC
    return bits


def _fileop_payload(self, command, drive, user, *, remote_name, new_name="", attributes=0):
    payload = bytearray(
        self._command_payload(command, drive, user, remote_name=remote_name)
    )
    if new_name:
        payload[26:37] = protocol.cpm_raw_name(new_name)
    payload[37] = attributes & 0x07
    return bytes(payload)


def _metadata_operation(
    self,
    command,
    drive,
    user,
    *,
    remote_name,
    new_name="",
    attributes=0,
    description="CP/M file operation",
):
    stats = TransferStats(bytes_in_file=0, mode="HOST2/FILEOP")
    self._begin()
    metadata = _fileop_payload(
        self,
        command,
        drive,
        user,
        remote_name=remote_name,
        new_name=new_name,
        attributes=attributes,
    )
    self._send_packet_with_retry(self._packet(0, metadata), 0, stats)

    while True:
        self._check_cancel()
        ch = self._read_one(self.response_timeout)
        if ch is None:
            self._cancel_remote()
            raise XModemError(f"Timed out waiting for {description}")
        if ch == protocol.EOT:
            self.ser.write(bytes([protocol.ACK]))
            self.ser.flush()
            return
        if ch == protocol.CAN:
            second = self._read_one(1.0)
            if second == protocol.CAN:
                raise XModemError(f"HOST.COM could not complete {description}")
            continue
        # Ignore stale ready advertisements or line noise while waiting for the
        # final status from the metadata-only operation.


def _delete_file(self, remote_name, drive, user):
    self.on_log(f"Deleting {remote_name} on CP/M {self._format_target(drive, user)}.")
    return _metadata_operation(
        self,
        self.CMD_DELETE,
        drive,
        user,
        remote_name=remote_name,
        description=f"delete of {remote_name}",
    )


def _rename_file(self, old_name, new_name, drive, user):
    self.on_log(
        f"Renaming {old_name} to {new_name} on CP/M {self._format_target(drive, user)}."
    )
    return _metadata_operation(
        self,
        self.CMD_RENAME,
        drive,
        user,
        remote_name=old_name,
        new_name=new_name,
        description=f"rename of {old_name}",
    )


def _set_attributes(self, remote_name, attributes, drive, user):
    labels = []
    if attributes & ATTR_RO:
        labels.append("R/O")
    if attributes & ATTR_SYS:
        labels.append("SYS")
    if attributes & ATTR_ARC:
        labels.append("ARC")
    shown = ", ".join(labels) if labels else "none"
    self.on_log(
        f"Setting {remote_name} attributes to {shown} on CP/M "
        f"{self._format_target(drive, user)}."
    )
    return _metadata_operation(
        self,
        self.CMD_ATTR,
        drive,
        user,
        remote_name=remote_name,
        attributes=attributes,
        description=f"attribute change for {remote_name}",
    )


HostLinkV2.delete_file = _delete_file
HostLinkV2.rename_file = _rename_file
HostLinkV2.set_file_attributes = _set_attributes


def _cpm_render(self, files):
    self.cselection.unselect_all()
    current._clear_store(self.cstore)
    self.cfiles = list(files)
    self.csel = None

    if not self.cfiles:
        self.cstore.append(current.PaneItem(name="No directory loaded", placeholder=True))
    else:
        for item in self.cfiles:
            detail = self.sz(item.size_bytes)
            if item.attributes:
                detail += f"  ·  {item.attributes}"
            self.cstore.append(
                current.PaneItem(
                    name=item.name,
                    detail=detail,
                    cpm_file=item,
                )
            )
    self.buttons()


current._cpm_render = _cpm_render


def _selected_cpm_items(self):
    return [
        item.cpm_file
        for item in current._selected_items(self.cselection, self.cstore)
        if item.cpm_file is not None
    ]


def _popover_button(label, callback, *, sensitive=True):
    button = Gtk.Button(label=label)
    button.set_sensitive(sensitive)
    button.set_halign(Gtk.Align.FILL)
    button.connect("clicked", callback)
    return button


def _show_cpm_menu(self, list_item, row_box, _x, _y):
    item = list_item.get_item()
    position = list_item.get_position()
    if self.busy or item is None or item.cpm_file is None:
        return

    if not self.cselection.is_selected(position):
        self.cselection.select_item(position, True)

    selected = _selected_cpm_items(self)
    if not selected:
        return

    popover = Gtk.Popover()
    popover.set_parent(row_box)
    menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    menu.set_margin_top(6)
    menu.set_margin_bottom(6)
    menu.set_margin_start(6)
    menu.set_margin_end(6)
    popover.set_child(menu)

    def invoke(callback):
        popover.popdown()
        callback()

    menu.append(
        _popover_button(
            "Rename…",
            lambda _button: invoke(lambda: _show_rename_dialog(self)),
            sensitive=len(selected) == 1,
        )
    )
    menu.append(
        _popover_button(
            "Attributes…",
            lambda _button: invoke(lambda: _show_attributes_dialog(self)),
            sensitive=len(selected) == 1,
        )
    )
    menu.append(
        _popover_button(
            "Delete…" if len(selected) == 1 else f"Delete {len(selected)} files…",
            lambda _button: invoke(lambda: _show_delete_dialog(self)),
        )
    )

    popover.connect("closed", lambda widget: widget.unparent())
    popover.popup()


def _setup_row(self, side, factory, list_item):
    _ORIGINAL_SETUP_ROW(self, side, factory, list_item)
    if side != "cpm":
        return
    row_box = list_item.get_child()
    click = Gtk.GestureClick()
    click.set_button(3)
    click.connect(
        "pressed",
        lambda _gesture, _count, x, y, li=list_item, box=row_box: _show_cpm_menu(
            self, li, box, x, y
        ),
    )
    row_box.add_controller(click)


current._setup_row = _setup_row


def _dialog_error(self, message):
    dialog = Adw.MessageDialog(
        transient_for=self,
        heading="CP/M file operation",
        body=str(message),
    )
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.present()


def _show_rename_dialog(self):
    selected = _selected_cpm_items(self)
    if len(selected) != 1 or self.busy:
        return
    item = selected[0]

    entry = Gtk.Entry()
    entry.set_text(item.name)
    entry.set_activates_default(True)
    entry.set_max_length(12)
    entry.set_margin_top(6)
    entry.set_margin_bottom(6)

    dialog = Adw.MessageDialog(
        transient_for=self,
        heading="Rename CP/M file",
        body=f"Rename {item.name} on the selected CP/M drive/user area.",
    )
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("rename", "Rename")
    dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("rename")
    dialog.set_close_response("cancel")

    def response(_dialog, name):
        if name != "rename":
            return
        try:
            new_name = _normalize_cpm_name(entry.get_text())
        except ValueError as exc:
            _dialog_error(self, exc)
            return
        if new_name == item.name.upper():
            return
        if any(
            other.name.upper() == new_name and other.name.upper() != item.name.upper()
            for other in self.cfiles
        ):
            _dialog_error(self, f"{new_name} already exists in this CP/M directory.")
            return
        _start_rename(self, item, new_name)

    dialog.connect("response", response)
    dialog.present()
    entry.grab_focus()
    entry.select_region(0, -1)


def _show_attributes_dialog(self):
    selected = _selected_cpm_items(self)
    if len(selected) != 1 or self.busy:
        return
    item = selected[0]
    bits = _attribute_bits(item.attributes)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    ro = Gtk.CheckButton(label="Read-only (R/O)")
    sys = Gtk.CheckButton(label="System (SYS)")
    arc = Gtk.CheckButton(label="Archive (ARC — CP/M 3)")
    arc.set_tooltip_text(
        "Archive is defined by CP/M 3. CP/M 2.2 formally guarantees R/O and SYS only."
    )
    ro.set_active(bool(bits & ATTR_RO))
    sys.set_active(bool(bits & ATTR_SYS))
    arc.set_active(bool(bits & ATTR_ARC))
    box.append(ro)
    box.append(sys)
    box.append(arc)

    dialog = Adw.MessageDialog(
        transient_for=self,
        heading="CP/M file attributes",
        body=item.name,
    )
    dialog.set_extra_child(box)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Apply")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    def response(_dialog, name):
        if name != "save":
            return
        new_bits = 0
        if ro.get_active():
            new_bits |= ATTR_RO
        if sys.get_active():
            new_bits |= ATTR_SYS
        if arc.get_active():
            new_bits |= ATTR_ARC
        if new_bits == bits:
            return
        _start_attributes(self, item, new_bits)

    dialog.connect("response", response)
    dialog.present()


def _show_delete_dialog(self):
    selected = _selected_cpm_items(self)
    if not selected or self.busy:
        return

    if len(selected) == 1:
        body = f"Permanently delete {selected[0].name} from CP/M?"
    else:
        body = f"Permanently delete {len(selected)} selected CP/M files?"
    if any(_attribute_bits(item.attributes) & ATTR_RO for item in selected):
        body += "\n\nOne or more selected files are read-only; CP/M may reject those deletes until R/O is cleared."

    dialog = Adw.MessageDialog(
        transient_for=self,
        heading="Delete CP/M file" if len(selected) == 1 else "Delete CP/M files",
        body=body,
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("delete", "Delete")
    dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect(
        "response",
        lambda _dialog, name: _start_delete(self, selected) if name == "delete" else None,
    )
    dialog.present()


def _start_delete(self, files):
    if self.busy or not files or not self.port():
        return
    count = len(files)
    self.begin(f"Deleting {files[0].name}…" if count == 1 else f"Deleting {count} files…")
    self.worker = threading.Thread(target=_delete_worker, args=(self, list(files)), daemon=True)
    self.worker.start()


def _delete_worker(self, files):
    completed = 0
    try:
        with self.ser() as ser:
            link = self.link(ser)
            for index, item in enumerate(files, start=1):
                GLib.idle_add(
                    self.status.set_text,
                    f"Deleting {item.name}…" if len(files) == 1 else f"Deleting {index}/{len(files)}: {item.name}",
                )
                link.delete_file(item.name, self.drive(), self.user())
                completed += 1
        GLib.idle_add(_fileop_done, self, "Delete complete" if len(files) == 1 else f"Deleted {len(files)} files")
    except Exception as exc:
        message = str(exc)
        if len(files) > 1:
            message = f"Delete stopped after {completed}/{len(files)} file(s): {message}"
        GLib.idle_add(_fileop_error, self, message)


def _start_rename(self, item, new_name):
    if self.busy or not self.port():
        return
    self.begin(f"Renaming {item.name}…")
    self.worker = threading.Thread(
        target=_rename_worker,
        args=(self, item.name, new_name),
        daemon=True,
    )
    self.worker.start()


def _rename_worker(self, old_name, new_name):
    try:
        with self.ser() as ser:
            self.link(ser).rename_file(old_name, new_name, self.drive(), self.user())
        GLib.idle_add(_fileop_done, self, f"Renamed {old_name} to {new_name}")
    except Exception as exc:
        GLib.idle_add(_fileop_error, self, str(exc))


def _start_attributes(self, item, bits):
    if self.busy or not self.port():
        return
    self.begin(f"Updating {item.name} attributes…")
    self.worker = threading.Thread(
        target=_attributes_worker,
        args=(self, item.name, bits),
        daemon=True,
    )
    self.worker.start()


def _attributes_worker(self, name, bits):
    try:
        with self.ser() as ser:
            self.link(ser).set_file_attributes(name, bits, self.drive(), self.user())
        GLib.idle_add(_fileop_done, self, f"Updated {name} attributes")
    except Exception as exc:
        GLib.idle_add(_fileop_error, self, str(exc))


def _refresh_after_fileop(self):
    if not self.busy and self.port():
        self.crefresh()
    return False


def _fileop_done(self, message):
    self.busy = False
    self.prog.set_fraction(1)
    self.status.set_text(message)
    self.log(message)
    self.buttons()
    GLib.timeout_add(100, _refresh_after_fileop, self)
    return False


def _fileop_error(self, message):
    self.err(message)
    GLib.timeout_add(100, _refresh_after_fileop, self)
    return False


def _key_pressed(self, _controller, keyval, _keycode, state):
    if self.busy:
        return False
    if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
        if _selected_cpm_items(self):
            _show_delete_dialog(self)
            return True
    if keyval == Gdk.KEY_F2 and not (state & Gdk.ModifierType.CONTROL_MASK):
        if len(_selected_cpm_items(self)) == 1:
            _show_rename_dialog(self)
            return True
    return False


def _fileops_init(self, app):
    _ORIGINAL_INIT(self, app)
    key = Gtk.EventControllerKey()
    key.connect("key-pressed", lambda controller, keyval, keycode, state: _key_pressed(self, controller, keyval, keycode, state))
    self.cview.add_controller(key)
    self.cview.set_tooltip_text(
        "Ctrl-click, Shift-click, Ctrl+A, or rubber-band to select multiple files; "
        "drag selected files to Linux. Right-click for Rename, Attributes, and Delete."
    )
    self.log("CP/M file management active: rename, attributes, delete")


base.ui.Win.__init__ = _fileops_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

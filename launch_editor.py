#!/usr/bin/env python3
"""External-editor layer for CP/M files in the production Host Link UI.

Right-click a CP/M file and choose Edit to receive it into a temporary Linux
working directory, open it with a configurable external editor, and upload the
changed file back to CP/M when the editor exits.  The replacement uses a
staged CP/M temporary file plus rename operations so the original is not
deleted until the edited upload has completed successfully.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import launch_fileops as production

current = production.current
base = production.base
Gtk = production.Gtk
GLib = production.GLib
Adw = production.Adw

_ORIGINAL_INIT = base.ui.Win.__init__
_ORIGINAL_KEY_PRESSED = production._key_pressed

EDITOR_KEY = "external_editor"
DEFAULT_EDITOR = "subl --wait"


def _configured_editor(self) -> str:
    value = str(self.cfg.get(EDITOR_KEY, "") or "").strip()
    if value:
        return value
    if shutil.which("subl"):
        return DEFAULT_EDITOR
    visual = os.environ.get("VISUAL", "").strip()
    if visual:
        return visual
    editor = os.environ.get("EDITOR", "").strip()
    if editor:
        return editor
    return DEFAULT_EDITOR


def _editor_argv(command: str, filename: Path) -> list[str]:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"Invalid editor command: {exc}") from exc
    if not argv:
        raise ValueError("Editor command is empty.")

    file_text = str(filename)
    replaced = False
    expanded = []
    for arg in argv:
        if "{file}" in arg:
            expanded.append(arg.replace("{file}", file_text))
            replaced = True
        else:
            expanded.append(arg)
    if not replaced:
        expanded.append(file_text)

    executable = expanded[0]
    if "/" in executable:
        if not Path(executable).expanduser().exists():
            raise ValueError(f"Editor executable was not found: {executable}")
        expanded[0] = str(Path(executable).expanduser())
    elif shutil.which(executable) is None:
        raise ValueError(
            f"Editor executable '{executable}' was not found. "
            "Choose Editor Settings from the CP/M file menu."
        )
    return expanded


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trim_text_eof_padding(path: Path) -> int:
    """Hide normal CP/M Ctrl-Z record padding from text editors.

    CP/M stores file sizes in 128-byte records, so ordinary text files often
    arrive with one or more trailing 1Ah bytes.  Only trim when the entire file
    looks like ASCII text; binary files are left byte-for-byte unchanged.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if not data or data[-1] != 0x1A or b"\x00" in data:
        return 0

    text_controls = {0x08, 0x09, 0x0A, 0x0C, 0x0D, 0x1A}
    if any(byte < 0x20 and byte not in text_controls for byte in data):
        return 0
    if any(byte > 0x7E for byte in data):
        return 0

    trimmed = data.rstrip(b"\x1a")
    removed = len(data) - len(trimmed)
    if removed:
        path.write_bytes(trimmed)
    return removed


def _work_names(self, original: str) -> tuple[str, str]:
    occupied = {item.name.upper() for item in self.cfiles}
    occupied.add(original.upper())
    for number in range(1000000):
        stem = f"HL{number:06d}"
        temp_name = f"{stem}.TMP"
        backup_name = f"{stem}.BAK"
        if temp_name not in occupied and backup_name not in occupied:
            return temp_name, backup_name
    raise RuntimeError("Could not allocate temporary CP/M filenames for editing.")


def _show_editor_settings(self):
    entry = Gtk.Entry()
    entry.set_text(_configured_editor(self))
    entry.set_hexpand(True)
    entry.set_activates_default(True)
    entry.set_margin_top(8)
    entry.set_margin_bottom(8)
    entry.set_tooltip_text(
        "Example: subl --wait. Host Link appends the temporary filename unless "
        "the command contains {file}."
    )

    dialog = Adw.MessageDialog(
        transient_for=self,
        heading="External editor",
        body=(
            "Command used to edit CP/M files. For GUI editors, include the option "
            "that waits until the file is closed. Sublime Text: subl --wait"
        ),
    )
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    def response(_dialog, name):
        if name != "save":
            return
        command = entry.get_text().strip()
        if not command:
            production._dialog_error(self, "Editor command cannot be empty.")
            return
        try:
            shlex.split(command)
        except ValueError as exc:
            production._dialog_error(self, f"Invalid editor command: {exc}")
            return
        self.cfg[EDITOR_KEY] = command
        base.ui.save_settings(self.cfg)
        self.log(f"External editor set to: {command}")
        self.status.set_text("Editor setting saved")

    dialog.connect("response", response)
    dialog.present()
    entry.grab_focus()
    entry.select_region(0, -1)


def _show_cpm_menu(self, list_item, row_box, _x, _y):
    item = list_item.get_item()
    position = list_item.get_position()
    if self.busy or item is None or item.cpm_file is None:
        return

    if not self.cselection.is_selected(position):
        self.cselection.select_item(position, True)

    selected = production._selected_cpm_items(self)
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
        production._popover_button(
            "Edit…",
            lambda _button: invoke(lambda: _start_edit(self)),
            sensitive=len(selected) == 1,
        )
    )
    menu.append(
        production._popover_button(
            "Rename…",
            lambda _button: invoke(lambda: production._show_rename_dialog(self)),
            sensitive=len(selected) == 1,
        )
    )
    menu.append(
        production._popover_button(
            "Attributes…",
            lambda _button: invoke(lambda: production._show_attributes_dialog(self)),
            sensitive=len(selected) == 1,
        )
    )
    menu.append(
        production._popover_button(
            "Delete…" if len(selected) == 1 else f"Delete {len(selected)} files…",
            lambda _button: invoke(lambda: production._show_delete_dialog(self)),
        )
    )

    separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    separator.set_margin_top(3)
    separator.set_margin_bottom(3)
    menu.append(separator)
    menu.append(
        production._popover_button(
            "Editor Settings…",
            lambda _button: invoke(lambda: _show_editor_settings(self)),
        )
    )

    popover.connect("closed", lambda widget: widget.unparent())
    popover.popup()


def _set_edit_status(self, text: str):
    self.status.set_text(text)
    return False


def _restore_original_after_failed_swap(link, original, backup, temp_name, attrs, drive, user):
    """Best-effort rollback before the edited file has been committed."""
    try:
        link.rename_file(backup, original, drive, user)
        if attrs:
            link.set_file_attributes(original, attrs, drive, user)
    except Exception as rollback_exc:
        link.on_log(
            f"WARNING: automatic rollback failed. Original content remains as "
            f"{backup}; edited upload remains as {temp_name}: {rollback_exc}"
        )
        return False
    try:
        link.delete_file(temp_name, drive, user)
    except Exception:
        pass
    return True


def _replace_remote_file(self, link, local_path, original, temp_name, backup, attrs, drive, user):
    """Upload edited content, then swap it into place with rollback protection."""
    link.on_log(f"Staging edited {original} as {temp_name}.")
    stats = link.send_file(str(local_path), temp_name, drive, user)

    cleared_read_only = False
    if attrs & production.ATTR_RO:
        link.set_file_attributes(original, attrs & ~production.ATTR_RO, drive, user)
        cleared_read_only = True

    try:
        link.rename_file(original, backup, drive, user)
    except Exception:
        if cleared_read_only:
            try:
                link.set_file_attributes(original, attrs, drive, user)
            except Exception as restore_exc:
                link.on_log(f"WARNING: could not restore original attributes: {restore_exc}")
        try:
            link.delete_file(temp_name, drive, user)
        except Exception:
            pass
        raise

    try:
        link.rename_file(temp_name, original, drive, user)
    except Exception:
        _restore_original_after_failed_swap(
            link, original, backup, temp_name, attrs, drive, user
        )
        raise

    warnings = []
    if attrs:
        try:
            link.set_file_attributes(original, attrs, drive, user)
        except Exception as exc:
            warnings.append(f"could not restore attributes: {exc}")

    try:
        link.delete_file(backup, drive, user)
    except Exception as exc:
        warnings.append(f"old copy remains as {backup}: {exc}")

    return stats, warnings


def _start_edit(self):
    selected = production._selected_cpm_items(self)
    if self.busy or len(selected) != 1 or not self.port():
        return

    item = selected[0]
    command = _configured_editor(self)
    try:
        _editor_argv(command, Path(item.name))
        temp_name, backup_name = _work_names(self, item.name)
    except Exception as exc:
        production._dialog_error(self, exc)
        _show_editor_settings(self)
        return

    drive = self.drive()
    user = self.user()
    attrs = production._attribute_bits(item.attributes)
    self.begin(f"Downloading {item.name} for editing…")
    self.worker = threading.Thread(
        target=_edit_worker,
        args=(
            self,
            item,
            command,
            drive,
            user,
            attrs,
            temp_name,
            backup_name,
        ),
        daemon=True,
    )
    self.worker.start()


def _edit_worker(self, item, command, drive, user, attrs, temp_name, backup_name):
    try:
        with tempfile.TemporaryDirectory(prefix="s100-host-link-edit-") as workdir:
            local_path = Path(workdir) / item.name

            with self.ser() as ser:
                link = self.link(ser)
                link.receive_file(
                    item.name,
                    str(local_path),
                    drive,
                    user,
                    item.size_bytes,
                )

            removed = _trim_text_eof_padding(local_path)
            if removed:
                GLib.idle_add(
                    self.log,
                    f"Removed {removed} trailing CP/M Ctrl-Z padding byte(s) for text editing.",
                )

            before = _hash_file(local_path)
            argv = _editor_argv(command, local_path)
            GLib.idle_add(
                _set_edit_status,
                self,
                f"Editing {item.name} — close the editor file to save back",
            )
            GLib.idle_add(
                self.log,
                "Opening editor: " + " ".join(shlex.quote(arg) for arg in argv),
            )

            completed = subprocess.run(argv, check=False)
            if completed.returncode:
                GLib.idle_add(
                    self.log,
                    f"Editor exited with status {completed.returncode}; checking for saved changes.",
                )

            after = _hash_file(local_path)
            if before == after:
                GLib.idle_add(_edit_done, self, item.name, False, [])
                return

            GLib.idle_add(_set_edit_status, self, f"Uploading edited {item.name}…")
            with self.ser() as ser:
                link = self.link(ser)
                _stats, warnings = _replace_remote_file(
                    self,
                    link,
                    local_path,
                    item.name,
                    temp_name,
                    backup_name,
                    attrs,
                    drive,
                    user,
                )

            GLib.idle_add(_edit_done, self, item.name, True, warnings)
    except Exception as exc:
        GLib.idle_add(_edit_error, self, str(exc))


def _refresh_after_edit(self):
    if not self.busy and self.port():
        self.crefresh()
    return False


def _edit_done(self, name, changed, warnings):
    self.busy = False
    self.prog.set_fraction(1)
    if not changed:
        message = f"No changes to {name}; CP/M file left unchanged"
    elif warnings:
        message = f"Saved edited {name} with warning"
    else:
        message = f"Saved edited {name} back to CP/M"
    self.status.set_text(message)
    self.log(message)
    for warning in warnings:
        self.log(f"WARNING: {warning}")
    self.buttons()
    GLib.timeout_add(150, _refresh_after_edit, self)
    return False


def _edit_error(self, message):
    self.err(f"CP/M edit failed: {message}")
    GLib.timeout_add(150, _refresh_after_edit, self)
    return False


def _key_pressed(self, controller, keyval, keycode, state):
    if (
        not self.busy
        and keyval in (ord("e"), ord("E"))
        and (state & production.Gdk.ModifierType.CONTROL_MASK)
    ):
        if len(production._selected_cpm_items(self)) == 1:
            _start_edit(self)
            return True
    return _ORIGINAL_KEY_PRESSED(self, controller, keyval, keycode, state)


def _editor_init(self, app):
    _ORIGINAL_INIT(self, app)
    self.cview.set_tooltip_text(
        "Ctrl-click, Shift-click, Ctrl+A, or rubber-band to select multiple files; "
        "drag selected files to Linux. Right-click for Edit, Rename, Attributes, "
        "Delete, and Editor Settings. Ctrl+E edits the selected CP/M file."
    )
    self.log(f"CP/M external editor active: {_configured_editor(self)}")


production._show_cpm_menu = _show_cpm_menu
production._key_pressed = _key_pressed
base.ui.Win.__init__ = _editor_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

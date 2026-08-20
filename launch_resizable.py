#!/usr/bin/env python3
"""Launch the dual-pane Host Link UI with a draggable Linux/CP/M divider.

This builds on launch_dualpane.py, which installs the GVFS/SMB-safe GET path,
and layers UI refinements over the development dual-pane window:

* native draggable Gtk.Paned divider
* reliable Linux/CP/M row selection using ListBox row indexes
* automatic, debounced CP/M directory refresh after drive/user changes
* manual refresh buttons for the Linux directory and serial-port list
* drag-and-drop-only file transfer controls; Send/Receive buttons are hidden
* startup settings restore without overwriting the saved baud rate
* double-click Linux directory navigation
"""
from __future__ import annotations

import launch_dualpane as base

Adw = base.ui.Adw
Gtk = base.ui.Gtk
GLib = base.ui.GLib

_ORIGINAL_INIT = base.ui.Win.__init__
_ORIGINAL_TARGET = base.ui.Win.target
_ORIGINAL_SAVE = base.ui.Win.save


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


def _install_usability_controls(self):
    """Add manual refresh controls and make drag-and-drop the transfer UI."""
    # Gtk.ListBox defaults to activate-on-single-click=True. Require activation
    # by double-click instead; the existing row-activated handler then descends
    # into directories with the normal GTK activation behavior.
    self.ll.set_activate_on_single_click(False)

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


def _resizable_init(self, app):
    # The base constructor does ui() -> refresh_ports() -> restore(). Selecting
    # a serial port during refresh_ports() emits notify::selected and calls
    # save(), while the baud widget is still at its default 9600. Suppress all
    # settings writes until restore() has finished so a saved 115200 (or any
    # other baud) is not overwritten during startup.
    self._suppress_settings_save = True
    self._suppress_target_refresh = True
    self._target_refresh_source = 0
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
base.ui.Win.__init__ = _resizable_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

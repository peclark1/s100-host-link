#!/usr/bin/env python3
"""Launch the dual-pane Host Link UI with a draggable Linux/CP/M divider.

This builds on launch_dualpane.py, which installs the GVFS/SMB-safe GET path,
and replaces the fixed two-frame horizontal container with a native Gtk.Paned
at runtime. Keeping this as a small development-layer patch lets us iterate on
the layout without disturbing the known transfer code.
"""
from __future__ import annotations

import launch_dualpane as base

Gtk = base.ui.Gtk
GLib = base.ui.GLib

_ORIGINAL_INIT = base.ui.Win.__init__


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


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


def _resizable_init(self, app):
    _ORIGINAL_INIT(self, app)

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
    # Do it on the idle loop so the initial layout remains balanced.
    def center_divider():
        width = paned.get_allocated_width()
        if width > 0:
            paned.set_position(width // 2)
            return False
        return True

    GLib.idle_add(center_divider)


base.ui.Win.__init__ = _resizable_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

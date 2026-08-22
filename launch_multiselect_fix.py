#!/usr/bin/env python3
"""Start Host Link with reliable multi-file drag selection preservation.

GTK ListBox can collapse a MULTIPLE selection to the row under the pointer when
that same button press starts a drag.  launch_resizable.py correctly enables
multi-selection and batch transfers, but its drag provider can therefore see
only the final row by the time Gtk.DragSource asks for content.

Keep the most recent larger selection alive briefly while a selection is
contracting.  A drag that begins from any member of that selection uses the
preserved group; an ordinary click that intentionally reduces the selection
settles to the new selection after the short grace period.
"""
from __future__ import annotations

import json
from pathlib import Path

import launch_resizable as multi

Gtk = multi.Gtk
GLib = multi.GLib

_SNAPSHOT_GRACE_MS = 1200


def _schedule_settle(self, side: str, token: int):
    def settle():
        if getattr(self, f"_{side}_snapshot_token", 0) != token:
            return False
        if side == "linux":
            current = multi._linux_selected_paths(self)
        else:
            current = multi._cpm_selected_files(self)
        setattr(self, f"_{side}_drag_snapshot", list(current))
        return False

    GLib.timeout_add(_SNAPSHOT_GRACE_MS, settle)


def _update_snapshot(self, side: str, current):
    """Track selection growth immediately, but delay selection contraction.

    A drag-starting pointer press is indistinguishable from an intentional
    single click at the ListBox selection-signal level.  Delaying contractions
    preserves the pre-drag group long enough for DragSource.prepare(), while a
    normal click still becomes authoritative shortly afterward.
    """
    current = list(current)
    attr = f"_{side}_drag_snapshot"
    token_attr = f"_{side}_snapshot_token"
    previous = list(getattr(self, attr, []))
    token = getattr(self, token_attr, 0) + 1
    setattr(self, token_attr, token)

    if side == "linux":
        current_keys = {str(item) for item in current}
        previous_keys = {str(item) for item in previous}
    else:
        current_keys = {item.name.upper() for item in current}
        previous_keys = {item.name.upper() for item in previous}

    contracting = (
        len(previous) > 1
        and len(current) < len(previous)
        and current_keys.issubset(previous_keys)
    )

    if contracting:
        _schedule_settle(self, side, token)
        return

    setattr(self, attr, current)


def _linux_selected(self, _listbox, _row=None):
    paths = multi._linux_selected_paths(self)
    _update_snapshot(self, "linux", paths)
    self.lsels = paths
    self.lsel = paths[0] if paths else None
    self.buttons()


def _cpm_selected(self, _listbox, _row=None):
    files = multi._cpm_selected_files(self)
    _update_snapshot(self, "cpm", files)
    self.csels = files
    self.csel = files[0] if files else None
    self.buttons()


def _provider(self, value):
    """Build drag content from the preserved pre-drag selection."""
    if isinstance(value, str) and value.startswith(self.LD):
        dragged = Path(value[len(self.LD):])
        snapshot = list(
            getattr(self, "_linux_drag_snapshot", multi._linux_selected_paths(self))
        )
        if len(snapshot) > 1 and any(Path(item) == dragged for item in snapshot):
            payload = self.LD + multi._BATCH_MARKER + json.dumps(
                [str(path) for path in snapshot]
            )
            self.log(f"Dragging {len(snapshot)} selected Linux files")
            return multi._ORIGINAL_PROVIDER(self, payload)

    if isinstance(value, str) and value.startswith(self.CD):
        dragged_name = value[len(self.CD):].upper()
        snapshot = list(
            getattr(self, "_cpm_drag_snapshot", multi._cpm_selected_files(self))
        )
        if len(snapshot) > 1 and any(
            item.name.upper() == dragged_name for item in snapshot
        ):
            payload = self.CD + multi._BATCH_MARKER + json.dumps(
                [item.name for item in snapshot]
            )
            self.log(f"Dragging {len(snapshot)} selected CP/M files")
            return multi._ORIGINAL_PROVIDER(self, payload)

    return multi._ORIGINAL_PROVIDER(self, value)


# Replace both the module globals used by launch_resizable's aggregate selection
# hooks and the class methods used by the ListBox row-selected signals.  This is
# done before the first window is created.
multi._linux_selected = _linux_selected
multi._cpm_selected = _cpm_selected
multi._provider = _provider
multi.base.ui.Win.lselected = _linux_selected
multi.base.ui.Win.cselected = _cpm_selected
multi.base.ui.Win.provider = _provider


if __name__ == "__main__":
    multi.base.ui.Adw.init()
    raise SystemExit(multi.base.ui.App().run(None))

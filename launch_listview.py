#!/usr/bin/env python3
"""Production Gtk.ListView/Gtk.MultiSelection Host Link UI.

The two file panes use model-backed Gtk.ListView widgets with Gtk.MultiSelection
as the authoritative selection model. Ctrl/Shift/Ctrl+A/rubber-band selection
therefore remains stable while dragging, and dragging any selected file copies
the entire selected set.
"""
from __future__ import annotations

import json
from pathlib import Path

import launch_resizable as current

base = current.base
Gtk = current.Gtk
Gdk = base.ui.Gdk
Gio = base.ui.Gio
GObject = base.ui.GObject

_BASE_INIT = current._resizable_init

_LINUX_BATCH = "HL:L:LISTVIEW:"
_CPM_BATCH = "HL:C:LISTVIEW:"


class PaneItem(GObject.Object):
    def __init__(
        self,
        *,
        name: str,
        detail: str = "",
        folder: bool = False,
        path: Path | None = None,
        cpm_file=None,
        placeholder: bool = False,
    ):
        super().__init__()
        self.name = name
        self.detail = detail
        self.folder = folder
        self.path = Path(path) if path is not None else None
        self.cpm_file = cpm_file
        self.placeholder = placeholder

    @property
    def transferable(self) -> bool:
        return not self.folder and not self.placeholder


def _clear_store(store):
    while store.get_n_items():
        store.remove(store.get_n_items() - 1)


def _selected_items(selection, store, *, transferable_only=True):
    items = []
    for position in range(store.get_n_items()):
        if not selection.is_selected(position):
            continue
        item = store.get_item(position)
        if item is None:
            continue
        if transferable_only and not item.transferable:
            continue
        items.append(item)
    return items


def _find_ancestor(widget, widget_type):
    parent = widget.get_parent() if widget is not None else None
    while parent is not None:
        if isinstance(parent, widget_type):
            return parent
        parent = parent.get_parent()
    return None


def _setup_row(self, side, _factory, list_item):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
    box.set_margin_top(5)
    box.set_margin_bottom(5)
    box.set_margin_start(7)
    box.set_margin_end(7)

    icon = Gtk.Image()
    box.append(icon)

    label = Gtk.Label(xalign=0)
    label.set_hexpand(True)
    label.set_ellipsize(3)
    box.append(label)

    detail = Gtk.Label(xalign=1)
    detail.add_css_class("dim-label")
    box.append(detail)

    drag = Gtk.DragSource()
    drag.set_actions(Gdk.DragAction.COPY)
    drag.connect(
        "prepare",
        lambda _source, _x, _y, li=list_item, pane=side: _prepare_drag(self, pane, li),
    )
    box.add_controller(drag)

    list_item.set_child(box)


def _bind_row(_self, _side, _factory, list_item):
    item = list_item.get_item()
    box = list_item.get_child()
    if item is None or box is None:
        return

    icon = box.get_first_child()
    label = icon.get_next_sibling() if icon is not None else None
    detail = label.get_next_sibling() if label is not None else None

    if icon is not None:
        if item.placeholder:
            icon.set_from_icon_name("dialog-information-symbolic")
        elif item.folder:
            icon.set_from_icon_name("folder-symbolic")
        else:
            icon.set_from_icon_name("text-x-generic-symbolic")
    if label is not None:
        label.set_text(item.name)
    if detail is not None:
        detail.set_text(item.detail)

    list_item.set_selectable(not item.placeholder)
    list_item.set_activatable(not item.placeholder)


def _make_factory(self, side):
    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda f, li: _setup_row(self, side, f, li))
    factory.connect("bind", lambda f, li: _bind_row(self, side, f, li))
    return factory


def _prepare_drag(self, side, list_item):
    item = list_item.get_item()
    position = list_item.get_position()
    if item is None or not item.transferable:
        return None

    if side == "linux":
        selection = self.lselection
        store = self.lstore
        chosen = _selected_items(selection, store) if selection.is_selected(position) else [item]
        paths = [str(entry.path) for entry in chosen if entry.path is not None]
        if not paths:
            return None
        payload = self.LD + paths[0] if len(paths) == 1 else _LINUX_BATCH + json.dumps(paths)
    else:
        selection = self.cselection
        store = self.cstore
        chosen = _selected_items(selection, store) if selection.is_selected(position) else [item]
        names = [entry.cpm_file.name for entry in chosen if entry.cpm_file is not None]
        if not names:
            return None
        payload = self.CD + names[0] if len(names) == 1 else _CPM_BATCH + json.dumps(names)

    return self.provider(payload)


def _on_linux_selection(self, *_args):
    selected = _selected_items(self.lselection, self.lstore)
    self.lsel = selected[0].path if selected else None
    count = len(selected)
    if not self.busy:
        self.status.set_text("Ready" if count == 0 else f"{count} Linux file(s) selected")
    self.buttons()


def _on_cpm_selection(self, *_args):
    selected = _selected_items(self.cselection, self.cstore)
    self.csel = selected[0].cpm_file if selected else None
    count = len(selected)
    if not self.busy:
        self.status.set_text("Ready" if count == 0 else f"{count} CP/M file(s) selected")
    self.buttons()


def _activate_linux(self, _view, position):
    item = self.lstore.get_item(position)
    if item is not None and item.folder and item.path is not None:
        self.setdir(item.path)


def _activate_cpm(_self, _view, _position):
    return


def _linux_refresh(self):
    self.path.set_text(str(self.ldir))
    self.lselection.unselect_all()
    _clear_store(self.lstore)
    self.lrows = {}
    self.lsel = None

    try:
        entries = sorted(
            self.ldir.iterdir(),
            key=lambda path: (not path.is_dir(), path.name.casefold()),
        )
    except OSError as exc:
        self.toast(exc)
        self.buttons()
        return

    for index, path in enumerate(entries):
        self.lrows[index] = path
        if path.is_dir():
            detail = "Folder"
        else:
            try:
                detail = self.sz(path.stat().st_size)
            except OSError:
                detail = "—"
        self.lstore.append(
            PaneItem(
                name=path.name,
                detail=detail,
                folder=path.is_dir(),
                path=path,
            )
        )

    self.buttons()


def _cpm_render(self, files):
    self.cselection.unselect_all()
    _clear_store(self.cstore)
    self.cfiles = list(files)
    self.csel = None

    if not self.cfiles:
        self.cstore.append(PaneItem(name="No directory loaded", placeholder=True))
    else:
        for item in self.cfiles:
            self.cstore.append(
                PaneItem(
                    name=item.name,
                    detail=self.sz(item.size_bytes),
                    cpm_file=item,
                )
            )

    self.buttons()


def _decode_batch(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    try:
        decoded = json.loads(value[len(prefix):])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded if isinstance(item, str) and item]


def _drop_on_cpm(self, _target, value, _x, _y):
    batch = _decode_batch(value, _LINUX_BATCH)
    if batch is not None:
        return current._start_send_batch(self, [Path(path) for path in batch])
    if not isinstance(value, str) or not value.startswith(self.LD):
        return False
    return current._start_send_batch(self, [Path(value[len(self.LD):])])


def _drop_on_linux(self, _target, value, _x, _y):
    batch = _decode_batch(value, _CPM_BATCH)
    if batch is not None:
        by_name = {item.name.upper(): item for item in self.cfiles}
        files = [by_name[name.upper()] for name in batch if name.upper() in by_name]
        return current._start_receive_batch(self, files)
    if not isinstance(value, str) or not value.startswith(self.CD):
        return False
    name = value[len(self.CD):]
    item = next((entry for entry in self.cfiles if entry.name.upper() == name.upper()), None)
    return current._start_receive_batch(self, [item] if item is not None else [])


def _install_listviews(self):
    # Gtk.ListBox may be wrapped in an implicit Gtk.Viewport by Gtk.ScrolledWindow.
    linux_scroll = _find_ancestor(self.ll, Gtk.ScrolledWindow)
    cpm_scroll = _find_ancestor(self.cl, Gtk.ScrolledWindow)
    if linux_scroll is None or cpm_scroll is None:
        self.log("WARNING: Could not locate file-pane scrollers for Gtk.ListView.")
        return False

    self.lstore = Gio.ListStore.new(PaneItem)
    self.cstore = Gio.ListStore.new(PaneItem)
    self.lselection = Gtk.MultiSelection.new(self.lstore)
    self.cselection = Gtk.MultiSelection.new(self.cstore)

    self.lselection.connect("selection-changed", lambda *args: _on_linux_selection(self, *args))
    self.cselection.connect("selection-changed", lambda *args: _on_cpm_selection(self, *args))

    self.lview = Gtk.ListView.new(self.lselection, _make_factory(self, "linux"))
    self.cview = Gtk.ListView.new(self.cselection, _make_factory(self, "cpm"))

    for view in (self.lview, self.cview):
        view.set_show_separators(True)
        view.set_enable_rubberband(True)
        view.set_single_click_activate(False)
        view.add_css_class("data-table")
        view.set_vexpand(True)

    self.lview.set_tooltip_text(
        "Ctrl-click, Shift-click, Ctrl+A, or rubber-band to select multiple files; "
        "drag any selected file to CP/M."
    )
    self.cview.set_tooltip_text(
        "Ctrl-click, Shift-click, Ctrl+A, or rubber-band to select multiple files; "
        "drag any selected file to Linux."
    )

    self.lview.connect("activate", lambda view, pos: _activate_linux(self, view, pos))
    self.cview.connect("activate", lambda view, pos: _activate_cpm(self, view, pos))

    linux_drop = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
    linux_drop.connect("drop", lambda target, value, x, y: _drop_on_linux(self, target, value, x, y))
    self.lview.add_controller(linux_drop)
    self._listview_linux_drop = linux_drop

    cpm_drop = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
    cpm_drop.connect("drop", lambda target, value, x, y: _drop_on_cpm(self, target, value, x, y))
    self.cview.add_controller(cpm_drop)
    self._listview_cpm_drop = cpm_drop

    linux_scroll.set_child(self.lview)
    cpm_scroll.set_child(self.cview)
    self.ll = self.lview
    self.cl = self.cview

    base.ui.Win.lrefresh = _linux_refresh
    base.ui.Win.crender = _cpm_render
    base.ui.Win.dropc = _drop_on_cpm
    base.ui.Win.dropl = _drop_on_linux

    _linux_refresh(self)
    _cpm_render(self, [])
    return True


def _listview_init(self, app):
    _BASE_INIT(self, app)
    if _install_listviews(self):
        self.log("Multi-file selection active: Gtk.ListView + Gtk.MultiSelection")


base.ui.Win.__init__ = _listview_init

if __name__ == "__main__":
    base.ui.Adw.init()
    raise SystemExit(base.ui.App().run(None))

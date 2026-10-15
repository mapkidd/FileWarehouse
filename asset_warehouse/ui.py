"""Tkinter desktop UI for Asset Warehouse (dark theme)."""

import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, ttk

from . import __version__, dialogs, theme
from .crate import LEVELS, CrateReader, collect_folder, format_time, pack_crate
from .errors import FormatError, PassphraseRequiredError, WarehouseError
from .iconset import Icons
from .manifest import Keyring, Manifest
from .pallet import PalletBuilder, PalletReader, address

NO_MANIFEST = "None (unencrypted)"
CRATE_TYPES = [("Crates", "*.crate"), ("All files", "*.*")]
PALLET_TYPES = [("Pallets", "*.pallet"), ("All files", "*.*")]
MANIFEST_TYPES = [("Manifests", "*.manifest"), ("All files", "*.*")]


def human(n) -> str:
    if not isinstance(n, (int, float)):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def fmt_meta(key, value):
    if "size" in key.lower() and isinstance(value, int):
        return f"{human(value)}  ({value:,} bytes)" if value >= 1024 else human(value)
    return str(value)


def same_path(a, b) -> bool:
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def make_tree(parent, columns, headings, widths, show="headings", height=8):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=columns, show=show, height=height)
    for i, (col, head, w) in enumerate(zip(columns, headings, widths)):
        tree.heading(col, text=head, anchor="w")
        tree.column(col, width=w, anchor="w", stretch=(i == 0 and show == "headings"))
    if show != "headings":
        tree.column("#0", width=260, stretch=True)
    ys = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=ys.set)
    tree.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, tree


def fill_meta(tree, meta: dict):
    tree.delete(*tree.get_children())
    for k, v in meta.items():
        tree.insert("", "end", values=(k, fmt_meta(k, v)))


def toolbar_button(parent, app, text, role, command, **kw):
    return ttk.Button(parent, text=text, command=command, **app.icons.kw(role, 16), **kw)


def options_grid(parent, rows):
    """Label/widget rows. Widgets are children of `parent`, gridded into an inner frame."""
    grid = ttk.Frame(parent)
    for r, (label, widget) in enumerate(rows):
        ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w", pady=3)
        widget.grid(in_=grid, row=r, column=1, sticky="ew", pady=3, padx=(10, 0))
        widget.lift(grid)  # created before the frame, so raise it above in stacking order
    grid.columnconfigure(1, weight=1)
    return grid


class InspectorHeader(ttk.Frame):
    """Big icon + title + path line at the top of an inspector panel."""

    def __init__(self, parent, app, empty_role, empty_text):
        super().__init__(parent)
        self.app, self.empty_role, self.empty_text = app, empty_role, empty_text
        self.icon = ttk.Label(self)
        self.icon.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.title = ttk.Label(self, font=theme.FONT_TITLE)
        self.title.grid(row=0, column=1, sticky="sw")
        self.sub = ttk.Label(self, style="Path.TLabel")
        self.sub.grid(row=1, column=1, sticky="nw")
        self.columnconfigure(1, weight=1)
        self.clear()

    def set(self, role, title, sub):
        self.icon.config(image=self.app.icons.get(role, 32) or "")
        self.title.config(text=title, style="TLabel")
        self.sub.config(text=sub)

    def clear(self):
        self.icon.config(image=self.app.icons.get(self.empty_role, 32) or "")
        self.title.config(text=self.empty_text, style="Muted.TLabel")
        self.sub.config(text="")


class ManifestPicker(ttk.Combobox):
    """Combobox listing loaded manifests plus 'None'."""

    def __init__(self, parent, app, allow_none=True, **kw):
        super().__init__(parent, state="readonly", width=34, **kw)
        self.app, self.allow_none, self._ids = app, allow_none, []
        app.keyring_listeners.append(self.refresh)
        self.refresh()

    def refresh(self):
        current = self.selected_id()
        had_manifests = len(self._ids) > (1 if self.allow_none else 0)
        manifests = list(self.app.keyring)
        self._ids = ([None] if self.allow_none else []) + [m.key_id for m in manifests]
        self["values"] = ([NO_MANIFEST] if self.allow_none else []) + \
                         [f"{m.name} ({m.key_id})" for m in manifests]
        if current in self._ids and (current is not None or had_manifests):
            self.current(self._ids.index(current))
        elif manifests:
            self.current(len(self._ids) - len(manifests))  # default to first manifest
        elif self._ids:
            self.current(0)

    def selected_id(self):
        i = self.current()
        return self._ids[i] if 0 <= i < len(self._ids) else None

    def selected_manifest(self):
        kid = self.selected_id()
        return self.app.keyring.get(kid) if kid else None


# ---------------------------------------------------------------------------
# Crates tab
# ---------------------------------------------------------------------------

class CratesTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.entries = {}  # iid -> (source, arc_path)
        self.reader = None
        self.reader_owned = False   # True if we opened the file and must close it
        self.reader_parent = None   # PalletReader this crate is read from, if any
        self.label = ""
        app.keyring_listeners.append(self.on_keyring_changed)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)
        pane.add(self._build_pack(pane), weight=1)
        pane.add(self._build_inspect(pane), weight=1)

    # --- pack side
    def _build_pack(self, parent):
        box = ttk.LabelFrame(parent, text=" Pack a crate ", padding=10)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        toolbar_button(bar, self.app, "Add Files…", "add_files", self.add_files).pack(side="left")
        toolbar_button(bar, self.app, "Add Folder…", "add_folder", self.add_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Remove", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=6)
        self.count_lbl = ttk.Label(bar, text="0 files", style="Muted.TLabel")
        self.count_lbl.pack(side="right")

        frame, self.pack_tree = make_tree(box, ("path", "size"), ("Path in crate", "Size"), (300, 90))
        frame.pack(fill="both", expand=True, pady=8)

        self.name_var, self.comment_var = tk.StringVar(), tk.StringVar()
        self.level_var = tk.StringVar(value="max")
        self.manifest_pick = ManifestPicker(box, self.app)
        options_grid(box, [
            ("Name", ttk.Entry(box, textvariable=self.name_var)),
            ("Comment", ttk.Entry(box, textvariable=self.comment_var)),
            ("Compression", ttk.Combobox(box, textvariable=self.level_var,
                                         values=list(LEVELS), state="readonly")),
            ("Encrypt with", self.manifest_pick),
        ]).pack(fill="x")

        ttk.Button(box, text="Pack Crate…", style="Accent.TButton", command=self.pack,
                   **self.app.icons.kw("pack_crate", 32)).pack(fill="x", pady=(10, 0))
        return box

    def _add(self, pairs):
        existing = {arc.lower() for _, arc in self.entries.values()}
        dupes = 0
        for src, arc in pairs:
            if arc.lower() in existing:
                dupes += 1
                continue
            existing.add(arc.lower())
            iid = self.pack_tree.insert("", "end", values=(arc, human(os.path.getsize(src))))
            self.entries[iid] = (src, arc)
        if not self.name_var.get() and self.entries:
            first = next(iter(self.entries.values()))[1]
            self.name_var.set(first.split("/")[0].rsplit(".", 1)[0])
        self.count_lbl.config(text=f"{len(self.entries)} files")
        if dupes:
            dialogs.warning(self, "Duplicates skipped", f"{dupes} file(s) already in the list were skipped.")

    def add_files(self):
        paths = filedialog.askopenfilenames(title="Add files to crate")
        self._add((p, os.path.basename(p)) for p in paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="Add folder to crate")
        if folder:
            self._add(collect_folder(folder))

    def remove_selected(self):
        for iid in self.pack_tree.selection():
            self.pack_tree.delete(iid)
            self.entries.pop(iid, None)
        self.count_lbl.config(text=f"{len(self.entries)} files")

    def clear(self):
        self.pack_tree.delete(*self.pack_tree.get_children())
        self.entries.clear()
        self.count_lbl.config(text="0 files")

    def pack(self):
        if not self.entries:
            return dialogs.info(self, "Nothing to pack", "Add some files first.")
        name = self.name_var.get().strip() or "crate"
        out = filedialog.asksaveasfilename(title="Save crate", defaultextension=".crate",
                                           initialfile=name + ".crate", filetypes=CRATE_TYPES)
        if not out:
            return
        manifest = self.manifest_pick.selected_manifest()
        if manifest is None and not dialogs.ask_yes_no(
                self, "No encryption", "This crate will NOT be encrypted. Continue?"):
            return
        self.app.release_path(out)
        entries, level, comment = list(self.entries.values()), self.level_var.get(), self.comment_var.get()

        def work(progress):
            return pack_crate(entries, out, name=name, manifest=manifest, level=level,
                              comment=comment, progress=progress)

        def done(st):
            ratio = st["crate_size"] / st["original_size"] * 100 if st["original_size"] else 0
            self.app.status(f"Packed {os.path.basename(out)}")
            dialogs.info(self, "Crate packed",
                         f"{st['files']} files\n{human(st['original_size'])} → {human(st['crate_size'])} "
                         f"({ratio:.1f}%)\n\n{out}", icon=self.app.icons.get("pack_crate", 32))
            self.open_path(out)

        self.app.run_task("Packing crate", work, done)

    # --- inspect side
    def _build_inspect(self, parent):
        box = ttk.LabelFrame(parent, text=" Inspect / unpack a crate ", padding=10)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        toolbar_button(bar, self.app, "Open Crate…", "open_crate", self.open_dialog).pack(side="left")
        self.unpack_btn = toolbar_button(bar, self.app, "Unpack…", "unpack_crate", self.unpack,
                                         state="disabled")
        self.unpack_btn.pack(side="left", padx=6)
        self.header = InspectorHeader(box, self.app, "empty_crate", "No crate open")
        self.header.pack(fill="x", pady=(10, 0))

        split = ttk.PanedWindow(box, orient="vertical")
        split.pack(fill="both", expand=True, pady=(8, 0))
        top, bottom = ttk.Frame(split), ttk.Frame(split)
        split.add(top, weight=0)
        split.add(bottom, weight=1)
        ttk.Label(top, text="METADATA", style="Section.TLabel").pack(anchor="w", pady=(0, 2))
        frame, self.meta_tree = make_tree(top, ("key", "value"), ("Property", "Value"), (140, 260), height=11)
        self.meta_tree.column("value", stretch=True)
        frame.pack(fill="both", expand=True)
        ttk.Label(bottom, text="CONTENTS", style="Section.TLabel").pack(anchor="w", pady=(8, 2))
        frame, self.files_tree = make_tree(bottom, ("path", "size", "mtime"),
                                           ("Path", "Size", "Modified"), (260, 80, 130))
        frame.pack(fill="both", expand=True)
        return box

    def open_dialog(self):
        path = filedialog.askopenfilename(title="Open crate", filetypes=CRATE_TYPES)
        if path:
            self.open_path(path)

    def open_path(self, path):
        def work(progress):
            r = CrateReader.open(path)
            try:
                return r, r.metadata(self.app.keyring)
            except BaseException:
                r.close()
                raise
        self.app.run_task("Opening crate", work,
                          lambda res: self.show_reader(*res, label=path, owned=True))

    def reset(self):
        if self.reader is not None and self.reader_owned:
            self.reader.close()
        self.reader, self.reader_owned, self.reader_parent = None, False, None
        self.header.clear()
        self.meta_tree.delete(*self.meta_tree.get_children())
        self.files_tree.delete(*self.files_tree.get_children())
        self.unpack_btn.config(state="disabled")

    def show_reader(self, reader, meta, label, owned, parent=None):
        if reader is not self.reader:
            self.reset()
        self.reader, self.reader_owned, self.reader_parent = reader, owned, parent
        self.label = label
        fill_meta(self.meta_tree, meta)
        self.files_tree.delete(*self.files_tree.get_children())
        if reader.unlocked:
            title = reader.index.get("name") or os.path.basename(label)
            self.header.set("crate", title, label)
            for f in reader.index["files"]:
                self.files_tree.insert("", "end", values=(f["path"], human(f["size"]),
                                                          format_time(f.get("mtime"))))
            self.unpack_btn.config(state="normal")
        else:
            self.header.set("crate_locked", "Locked crate", label)
            self.files_tree.insert("", "end", values=("[locked] load the required manifest to view contents", "", ""))
            self.unpack_btn.config(state="disabled")
        self.app.status(f"Opened {label}")

    def on_keyring_changed(self):
        r = self.reader
        if r is not None and not r.unlocked and r.can_unlock(self.app.keyring):
            self.show_reader(r, r.metadata(self.app.keyring), self.label, self.reader_owned, self.reader_parent)

    def unpack(self):
        if not self.reader:
            return
        dest = filedialog.askdirectory(title="Unpack crate into…")
        if not dest or not self.app.confirm_dest(dest):
            return
        reader = self.reader

        def work(progress):
            return reader.extract_all(dest, self.app.keyring, progress)

        def done(n):
            self.app.status(f"Unpacked {n} files")
            dialogs.info(self, "Unpacked", f"Extracted {n} files to\n{dest}",
                         icon=self.app.icons.get("unpack_crate", 32))

        self.app.run_task("Unpacking crate", work, done)


# ---------------------------------------------------------------------------
# Pallets tab
# ---------------------------------------------------------------------------

class PalletsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.builder = PalletBuilder()
        self.reader = None
        self.view_items = {}  # iid -> crate entry (inspector)
        app.keyring_listeners.append(self.on_keyring_changed)

        pane = ttk.PanedWindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)
        pane.add(self._build_builder(pane), weight=1)
        pane.add(self._build_inspect(pane), weight=1)
        self.refresh_builder()

    def icon(self, role):
        return self.app.icons.get(role, 16) or ""

    # --- builder side
    def _build_builder(self, parent):
        box = ttk.LabelFrame(parent, text=" Build a pallet ", padding=10)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        toolbar_button(bar, self.app, "Add Node…", "add_node", self.add_node).pack(side="left")
        toolbar_button(bar, self.app, "Add Crates…", "add_crates", self.add_crates).pack(side="left", padx=6)
        toolbar_button(bar, self.app, "Folder Tree…", "folder_tree", self.add_folder_tree).pack(side="left")
        ttk.Button(bar, text="Remove", command=self.remove_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left")
        ttk.Label(box, text="Select a node, then add crates to it. Folder Tree turns each folder "
                            "into a node holding one crate of its files.",
                  style="Muted.TLabel", wraplength=440).pack(fill="x", pady=(6, 0))

        frame, self.build_tree = make_tree(box, ("type", "size", "manifest"),
                                           ("Type", "Size", "Crate manifest"), (70, 80, 120),
                                           show="tree headings")
        self.build_tree.heading("#0", text="Hierarchy", anchor="w")
        frame.pack(fill="both", expand=True, pady=8)

        self.name_var, self.comment_var = tk.StringVar(value="pallet"), tk.StringVar()
        self.level_var = tk.StringVar(value="max")
        self.manifest_pick = ManifestPicker(box, self.app)
        self.crate_manifest_pick = ManifestPicker(box, self.app)
        options_grid(box, [
            ("Pallet name", ttk.Entry(box, textvariable=self.name_var)),
            ("Comment", ttk.Entry(box, textvariable=self.comment_var)),
            ("Encrypt pallet with", self.manifest_pick),
            ("Folder Tree crates: key", self.crate_manifest_pick),
            ("Folder Tree crates: compression",
             ttk.Combobox(box, textvariable=self.level_var, values=list(LEVELS), state="readonly")),
        ]).pack(fill="x")
        self.name_var.trace_add("write", lambda *a: self.refresh_builder())

        ttk.Button(box, text="Pack Pallet…", style="Accent.TButton", command=self.pack,
                   **self.app.icons.kw("pack_pallet", 32)).pack(fill="x", pady=(10, 0))
        return box

    def refresh_builder(self):
        t = self.build_tree
        open_state = {iid: t.item(iid, "open") for iid in self._all_iids(t)}
        t.delete(*t.get_children())
        t.insert("", "end", iid="n:", text=f" root  ({self.name_var.get() or 'pallet'}.pallet)",
                 image=self.icon("pallet"), values=("pallet", "", ""), open=True)
        for node in sorted(self.builder.nodes, key=lambda n: n.lower()):
            parent = "n:" + node.rsplit(".", 1)[0] if "." in node else "n:"
            t.insert(parent, "end", iid="n:" + node, text=" " + node.rsplit(".", 1)[-1],
                     image=self.icon("node"), values=("node", "", ""),
                     open=open_state.get("n:" + node, True))
        for c in sorted(self.builder.crates, key=lambda c: c["name"].lower()):
            t.insert("n:" + c["location"], "end", iid=f"c:{c['location']}|{c['name']}",
                     text=f" {c['name']}.crate", image=self.icon("crate"),
                     values=("crate", human(c["size"]), c["manifest"] or "unencrypted"))

    @staticmethod
    def _all_iids(tree, parent=""):
        for iid in tree.get_children(parent):
            yield iid
            yield from PalletsTab._all_iids(tree, iid)

    def selected_location(self):
        sel = self.build_tree.selection()
        if not sel:
            return ""
        iid = sel[0]
        if iid.startswith("c:"):
            return iid[2:].split("|", 1)[0]
        return iid[2:]

    def add_node(self):
        loc = self.selected_location()
        path = dialogs.ask_string(self, "Add node", "Dotted node path (e.g. root.assets.images):",
                                  initial=address(loc) + ".")
        if not path:
            return
        try:
            self.builder.add_node(path)
        except ValueError as e:
            return dialogs.error(self, "Invalid path", str(e))
        self.refresh_builder()

    def add_crates(self):
        loc = self.selected_location()
        paths = filedialog.askopenfilenames(title=f"Add crates to {address(loc)}", filetypes=CRATE_TYPES)
        errors = []
        for p in paths:
            try:
                self.builder.add_crate_file(p, loc)
            except (ValueError, WarehouseError, OSError) as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        self.refresh_builder()
        if errors:
            dialogs.error(self, "Some crates were not added", "\n".join(errors))

    def add_folder_tree(self):
        loc = self.selected_location()
        folder = filedialog.askdirectory(title=f"Mirror folder into {address(loc)}")
        if not folder:
            return
        manifest, level = self.crate_manifest_pick.selected_manifest(), self.level_var.get()

        def work(progress):
            return self.builder.add_folder_tree(folder, loc, manifest, level, progress)

        def done(n):
            self.refresh_builder()
            self.app.status(f"Created {n} crates from {folder}")

        def failed(e):  # crates made before the failure are kept; show them
            self.refresh_builder()
            return False

        self.app.run_task("Crating folder tree", work, done, on_error=failed)

    def remove_selected(self):
        for iid in self.build_tree.selection():
            if iid.startswith("c:"):
                loc, name = iid[2:].split("|", 1)
                self.builder.remove_crate(loc, name)
            elif self.build_tree.exists(iid):
                self.builder.remove_node(iid[2:])
        self.refresh_builder()

    def clear(self):
        self.builder.remove_node("")
        self.builder.cleanup()
        self.refresh_builder()

    def pack(self):
        if not self.builder.crates and not self.builder.nodes:
            return dialogs.info(self, "Empty pallet", "Add some nodes or crates first.")
        name = self.name_var.get().strip() or "pallet"
        out = filedialog.asksaveasfilename(title="Save pallet", defaultextension=".pallet",
                                           initialfile=name + ".pallet", filetypes=PALLET_TYPES)
        if not out:
            return
        manifest = self.manifest_pick.selected_manifest()
        if manifest is None and not dialogs.ask_yes_no(
                self, "No encryption", "The pallet's hierarchy will NOT be encrypted "
                                       "(crates inside keep their own encryption). Continue?"):
            return
        self.app.release_path(out)
        self.builder.name, self.builder.comment = name, self.comment_var.get()

        def done(st):
            self.app.status(f"Packed {os.path.basename(out)}")
            dialogs.info(self, "Pallet packed", f"{st['crates']} crates in {st['nodes']} nodes\n"
                                                f"{human(st['pallet_size'])}\n\n{out}",
                         icon=self.app.icons.get("pack_pallet", 32))
            self.open_path(out)

        self.app.run_task("Packing pallet", lambda progress: self.builder.pack(out, manifest, progress), done)

    # --- inspect side
    def _build_inspect(self, parent):
        box = ttk.LabelFrame(parent, text=" Inspect / unpack a pallet ", padding=10)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        toolbar_button(bar, self.app, "Open…", "open_pallet", self.open_dialog).pack(side="left")
        self.unpack_btn = toolbar_button(bar, self.app, "Unpack…", "unpack_pallet", self.unpack,
                                         state="disabled")
        self.unpack_btn.pack(side="left", padx=6)
        self.extract_btn = toolbar_button(bar, self.app, "Extract Crate…", "extract_crate",
                                          self.extract_crate, state="disabled")
        self.extract_btn.pack(side="left")
        self.inspect_btn = toolbar_button(bar, self.app, "Inspect", "inspect_crate",
                                          self.inspect_crate, state="disabled")
        self.inspect_btn.pack(side="left", padx=6)
        self.contents_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="When unpacking, also unpack crate contents (needs their manifests)",
                        variable=self.contents_var).pack(anchor="w", pady=(8, 0))
        self.header = InspectorHeader(box, self.app, "empty_pallet", "No pallet open")
        self.header.pack(fill="x", pady=(8, 0))

        split = ttk.PanedWindow(box, orient="vertical")
        split.pack(fill="both", expand=True, pady=(8, 0))
        top, bottom = ttk.Frame(split), ttk.Frame(split)
        split.add(top, weight=0)
        split.add(bottom, weight=1)
        ttk.Label(top, text="METADATA", style="Section.TLabel").pack(anchor="w", pady=(0, 2))
        frame, self.meta_tree = make_tree(top, ("key", "value"), ("Property", "Value"), (140, 260), height=11)
        self.meta_tree.column("value", stretch=True)
        frame.pack(fill="both", expand=True)
        ttk.Label(bottom, text="HIERARCHY", style="Section.TLabel").pack(anchor="w", pady=(8, 2))
        frame, self.view_tree = make_tree(bottom, ("type", "size", "address"), ("Type", "Size", "Address"),
                                          (60, 80, 200), show="tree headings")
        self.view_tree.heading("#0", text="Name", anchor="w")
        self.view_tree.column("#0", width=300)
        self.view_tree.bind("<<TreeviewSelect>>", self.on_view_select)
        self.view_tree.bind("<Double-1>", lambda e: self.inspect_crate())
        frame.pack(fill="both", expand=True)
        return box

    def open_dialog(self):
        path = filedialog.askopenfilename(title="Open pallet", filetypes=PALLET_TYPES)
        if path:
            self.open_path(path)

    def _load(self, reader):
        """Worker: gather everything needed to draw the inspector. Only the
        pallet index and each crate's own index are decrypted - not file data."""
        kr = self.app.keyring
        meta = reader.metadata(kr)
        crates = {}
        if reader.unlocked:
            for i, e in enumerate(reader.index["crates"]):
                cr = reader.crate_reader(e)
                crates[i] = cr.unlock(kr)["files"] if cr.can_unlock(kr) else None
        return reader, meta, crates

    def open_path(self, path):
        def work(progress):
            r = PalletReader.open(path)
            try:
                return self._load(r)
            except BaseException:
                r.close()
                raise
        self.app.run_task("Opening pallet", work, lambda res: self.show_reader(*res))

    def reset(self):
        if self.reader is not None:
            if self.app.crates_tab.reader_parent is self.reader:
                self.app.crates_tab.reset()
            self.reader.close()
        self.reader = None
        self.view_items.clear()
        self.header.clear()
        self.meta_tree.delete(*self.meta_tree.get_children())
        self.view_tree.delete(*self.view_tree.get_children())
        self.unpack_btn.config(state="disabled")
        self.on_view_select()

    def show_reader(self, reader, meta, crate_files):
        if reader is not self.reader:
            self.reset()
        self.reader = reader
        fill_meta(self.meta_tree, meta)
        t = self.view_tree
        t.delete(*t.get_children())
        self.view_items.clear()
        self.on_view_select()
        src = reader.source or ""
        if not reader.unlocked:
            self.header.set("crate_locked", "Locked pallet", src)
            t.insert("", "end", text=" [locked] load the pallet's manifest to view its hierarchy",
                     image=self.icon("crate_locked"), values=("", "", ""))
            self.unpack_btn.config(state="disabled")
            return
        idx = reader.index
        self.header.set("pallet", idx.get("name") or os.path.basename(src), src)
        t.insert("", "end", iid="n:", text=f" root  ({idx.get('name', '')})", image=self.icon("pallet"),
                 values=("pallet", "", "root"), open=True)
        for node in sorted(idx["nodes"], key=str.lower):
            parent = "n:" + node.rsplit(".", 1)[0] if "." in node else "n:"
            t.insert(parent, "end", iid="n:" + node, text=" " + node.rsplit(".", 1)[-1],
                     image=self.icon("node"), values=("node", "", address(node)), open=True)
        for i, e in enumerate(idx["crates"]):
            parent = "n:" + e["location"]
            if not t.exists(parent):  # tolerate indexes missing a node entry
                parent = "n:"
            files = crate_files.get(i)
            locked = files is None
            iid = t.insert(parent, "end", text=f" {e['name']}.crate" + ("  [locked]" if locked else ""),
                           image=self.icon("crate_locked" if locked else "crate"), open=False,
                           values=("crate", human(e["size"]), address(e["location"], e["name"])))
            self.view_items[iid] = e
            if locked:
                t.insert(iid, "end", text=f" needs manifest '{e.get('manifest')}'", values=("", "", ""))
            else:
                for f in files:
                    t.insert(iid, "end", text=" " + f["path"], image=self.icon("file"), values=(
                        "file", human(f["size"]), address(e["location"], f["path"].replace("/", "."))))
        self.unpack_btn.config(state="normal")

    def on_view_select(self, event=None):
        state = "normal" if self.selected_view_crate() else "disabled"
        self.extract_btn.config(state=state)
        self.inspect_btn.config(state=state)

    def selected_view_crate(self):
        for iid in self.view_tree.selection():
            while iid:
                if iid in self.view_items:
                    return self.view_items[iid]
                iid = self.view_tree.parent(iid)
        return None

    def on_keyring_changed(self):
        if self.reader is not None:
            self.app.run_task("Refreshing pallet", lambda progress: self._load(self.reader),
                              lambda res: self.show_reader(*res), quiet=True)

    def unpack(self):
        if not self.reader:
            return
        dest = filedialog.askdirectory(title="Unpack pallet into…")
        if not dest or not self.app.confirm_dest(dest):
            return
        reader, contents = self.reader, self.contents_var.get()

        def work(progress):
            return reader.extract(dest, self.app.keyring, contents, progress)

        def done(rep):
            msg = (f"Crates unpacked: {rep['crates_unpacked']} ({rep['files']} files)\n"
                   f"Crates written as .crate: {rep['crates_written']}\n\n{dest}")
            if rep["skipped"]:
                msg += "\n\nKept as .crate (manifest not loaded):\n" + "\n".join(rep["skipped"][:15])
            self.app.status("Pallet unpacked")
            dialogs.info(self, "Pallet unpacked", msg, icon=self.app.icons.get("unpack_pallet", 32))

        self.app.run_task("Unpacking pallet", work, done)

    def extract_crate(self):
        e = self.selected_view_crate()
        if not e:
            return
        out = filedialog.asksaveasfilename(title="Extract crate", defaultextension=".crate",
                                           initialfile=e["name"] + ".crate", filetypes=CRATE_TYPES)
        if not out:
            return
        self.app.release_path(out)
        reader, addr, total = self.reader, address(e["location"], e["name"]), e["size"] or 1

        def work(progress):
            done = 0

            def tick(n):
                nonlocal done
                done += n
                progress(f"Extracting {addr}", done / total)
            reader.copy_crate(e, out, tick)

        self.app.run_task("Extracting crate", work,
                          lambda _: self.app.status(f"Extracted {addr} → {out}"))

    def inspect_crate(self):
        e = self.selected_view_crate()
        if not e:
            return
        tab = self.app.crates_tab
        reader = self.reader.crate_reader(e)
        tab.show_reader(reader, reader.metadata(self.app.keyring),
                        label=f"{address(e['location'], e['name'])}  (inside "
                              f"{os.path.basename(self.reader.source or '')})",
                        owned=False, parent=self.reader)
        self.app.notebook.select(tab)


# ---------------------------------------------------------------------------
# Manifests tab
# ---------------------------------------------------------------------------

class ManifestsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        intro = ttk.Frame(self)
        intro.pack(fill="x")
        ttk.Label(intro, image=app.icons.get("manifest", 32) or "").pack(side="left", padx=(0, 12))
        ttk.Label(intro, wraplength=1000, justify="left", style="Muted.TLabel", text=(
            "Manifests are keys. Load a manifest to open crates and pallets encrypted with it; "
            "any manifest loaded here can be chosen when packing. Each manifest holds a random "
            "256-bit key and can be protected with a passphrase (scrypt + AES-256-GCM). "
            "Lose the manifest (or its passphrase) and the data it encrypted is unrecoverable.")
        ).pack(side="left", fill="x", expand=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=10)
        toolbar_button(bar, app, "New Manifest…", "new_manifest", self.new_manifest).pack(side="left")
        toolbar_button(bar, app, "Load Manifest…", "load_manifest", self.load_manifest).pack(side="left", padx=6)
        ttk.Button(bar, text="Unload", command=self.unload).pack(side="left")
        frame, self.tree = make_tree(self, ("key_id", "protected", "created", "path"),
                                     ("Key ID", "Passphrase", "Created", "File"),
                                     (180, 100, 230, 300), show="tree headings", height=12)
        self.tree.heading("#0", text="Name", anchor="w")
        self.tree.column("#0", width=220, stretch=False)
        self.tree.column("path", stretch=True)
        frame.pack(fill="both", expand=True)
        app.keyring_listeners.append(self.refresh)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for m in self.app.keyring:
            self.tree.insert("", "end", iid=m.key_id, text=" " + m.name,
                             image=self.app.icons.get("manifest", 16) or "",
                             values=(m.key_id, "yes" if m.protected else "no", m.created, m.path or ""))

    def new_manifest(self):
        name = dialogs.ask_string(self, "New manifest", "Manifest name:")
        if not name:
            return
        pw = dialogs.ask_string(self, "Passphrase", "Optional passphrase to protect the manifest "
                                                    "(leave empty for none):", show="•")
        if pw is None:
            return
        if pw and dialogs.ask_string(self, "Passphrase", "Confirm passphrase:", show="•") != pw:
            return dialogs.error(self, "Mismatch", "Passphrases did not match.")
        path = filedialog.asksaveasfilename(title="Save manifest", defaultextension=".manifest",
                                            initialfile=name + ".manifest", filetypes=MANIFEST_TYPES)
        if not path:
            return
        m = Manifest.create(name)

        def done(_):
            self.app.add_manifest(m)
            dialogs.info(self, "Manifest created",
                         f"'{name}' saved to\n{path}\n\nKeep a backup — without it, "
                         f"anything encrypted with it cannot be opened.",
                         icon=self.app.icons.get("manifest", 32))

        self.app.run_task("Creating manifest", lambda progress: m.save(path, pw or None), done)

    def load_manifest(self):
        for path in filedialog.askopenfilenames(title="Load manifest", filetypes=MANIFEST_TYPES):
            self.app.load_manifest_path(path)

    def unload(self):
        for iid in self.tree.selection():
            self.app.keyring.remove(iid)
        self.app.keyring_changed()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()  # build fully, then show (lets the dark title bar apply cleanly)
        self.title(f"Asset Warehouse {__version__}")
        self.geometry("1320x840")
        self.minsize(1050, 680)
        self.keyring = Keyring()
        self.keyring_listeners = []
        self.busy = False
        self._q = queue.Queue()

        theme.apply(self)
        self.icons = Icons(self)
        app_icons = [i for i in (self.icons.get("app", 32), self.icons.get("app", 16)) if i]
        if app_icons:
            self.iconphoto(True, *app_icons)

        self._build_status_bar()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 8))
        self.crates_tab = CratesTab(self.notebook, self)
        self.pallets_tab = PalletsTab(self.notebook, self)
        self.manifests_tab = ManifestsTab(self.notebook, self)
        for tab, text, role in ((self.crates_tab, "Crates", "tab_crates"),
                                (self.pallets_tab, "Pallets", "tab_pallets"),
                                (self.manifests_tab, "Manifests", "tab_manifests")):
            self.notebook.add(tab, text=f" {text} ", **self.icons.kw(role, 16))
        self.keyring_changed()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        theme.dark_titlebar(self)
        self.deiconify()
        self.after(100, self._poll)

    def _build_status_bar(self):
        bar = ttk.Frame(self, padding=(10, 5), style="Status.TFrame")
        bar.pack(side="bottom", fill="x")
        self.status_lbl = ttk.Label(bar, text="Ready", style="Status.TLabel", compound="left")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.keys_lbl = ttk.Label(bar, style="Keys.TLabel", **self.icons.kw("key", 16))
        self.keys_lbl.pack(side="right", padx=(12, 0))
        self.progress = ttk.Progressbar(bar, mode="determinate", length=180)
        self.progress.pack(side="right")
        self.keyring_listeners.append(
            lambda: self.keys_lbl.config(text=f" {len(self.keyring)} manifest(s) loaded"))

    # keyring
    def keyring_changed(self):
        for fn in list(self.keyring_listeners):
            fn()

    def add_manifest(self, m):
        self.keyring.add(m)
        self.keyring_changed()
        self.status(f"Loaded manifest '{m.name}' ({m.key_id})")

    def load_manifest_path(self, path):
        try:
            protected = Manifest.is_protected(path)
        except FormatError as e:
            return dialogs.error(self, "Cannot load manifest", f"{os.path.basename(path)}: {e}")
        pw = None
        if protected:
            pw = dialogs.ask_string(self, "Passphrase", f"Passphrase for {os.path.basename(path)}:", show="•")
            if pw is None:
                return

        def fail(e):
            if isinstance(e, PassphraseRequiredError):
                dialogs.error(self, "Wrong passphrase", str(e))
                return True
            return False

        self.run_task("Unlocking manifest", lambda progress: Manifest.load(path, pw),
                      self.add_manifest, on_error=fail)

    # helpers
    def status(self, msg):
        self.status_lbl.config(text=msg)

    def confirm_dest(self, dest):
        if os.path.isdir(dest) and os.listdir(dest):
            return dialogs.ask_yes_no(self, "Folder not empty",
                                      f"{dest} is not empty.\nFiles with the same names will be "
                                      f"overwritten. Continue?")
        return True

    def release_path(self, path):
        """Close any open reader on `path` so it can be overwritten (Windows locks open files)."""
        if self.crates_tab.reader is not None and same_path(self.crates_tab.reader.source, path):
            self.crates_tab.reset()
        if self.pallets_tab.reader is not None and same_path(self.pallets_tab.reader.source, path):
            self.pallets_tab.reset()

    def on_close(self):
        if self.busy and not dialogs.ask_yes_no(
                self, "Operation running", "An operation is still running. Quit anyway?"):
            return
        self.crates_tab.reset()
        self.pallets_tab.reset()
        self.pallets_tab.builder.cleanup()
        self.destroy()

    # background tasks: work(progress) runs in a thread, done(result) on the UI thread.
    # The worker may call progress(message, fraction=None) as often as it likes.
    def run_task(self, label, work, done, on_error=None, quiet=False):
        if self.busy:
            if not quiet:
                dialogs.info(self, "Busy", "Please wait for the current operation to finish.")
            return
        self.busy = True
        self.status(label + "…")
        self.status_lbl.config(image=self.icons.get("busy", 16) or "")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)

        def progress(msg, fraction=None):
            self._q.put(("progress", msg, fraction))

        def target():
            try:
                self._q.put(("done", done, work(progress)))
            except Exception as e:  # noqa: BLE001 - reported to the user
                self._q.put(("error", on_error, e, traceback.format_exc()))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self):
        latest = None
        try:
            while True:
                item = self._q.get_nowait()
                if item[0] == "progress":
                    latest = item  # only the newest progress update matters
                    continue
                if latest is not None:
                    self._show_progress(latest)
                    latest = None
                self._finish(item)
        except queue.Empty:
            pass
        if latest is not None and self.busy:
            self._show_progress(latest)
        self.after(80, self._poll)

    def _show_progress(self, item):
        _, msg, frac = item
        self.status(msg)
        if frac is not None:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate")
            self.progress.config(value=max(0.0, min(100.0, frac * 100)))

    def _finish(self, item):
        self.busy = False
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.status_lbl.config(image="")
        if item[0] == "done":
            self.status("Ready")
            item[1](item[2])
        else:
            _, on_error, exc, tb = item
            self.status(f"Error: {exc}")
            if not (on_error and on_error(exc)):
                detail = "" if isinstance(exc, (WarehouseError, ValueError, OSError)) else "\n\n" + tb
                dialogs.error(self, "Error", f"{exc}{detail}")


def main():
    try:  # crisp text on high-DPI Windows displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not Windows / not available
        pass
    App().mainloop()


if __name__ == "__main__":
    main()

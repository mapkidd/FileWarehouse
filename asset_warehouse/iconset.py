"""Loads the pixel-art PNGs from the project's icons/ folder.

The art is drawn with 2px lines, so halving it (Tk's nearest-neighbour
subsample) stays crisp; other ratios would blur, so sizes snap to integer
factors of the source. Missing files just mean no icon - never an error.
"""

import os
import tkinter as tk

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")

# role -> file name (without .png). Change the art for a role in one place.
ROLES = {
    "app": "crate",
    "tab_crates": "crate", "tab_pallets": "pallet", "tab_manifests": "datacard",
    "add_files": "objects", "add_folder": "crate-avatar",
    "pack_crate": "crate-add", "open_crate": "crate", "unpack_crate": "crate-firework",
    "add_node": "barcode", "add_crates": "crate-add", "folder_tree": "palletjack",
    "pack_pallet": "pallet-add-big", "open_pallet": "pallet", "unpack_pallet": "packed-pallet",
    "extract_crate": "crate-ball", "inspect_crate": "crate-level",
    "new_manifest": "datacard", "load_manifest": "palletmanifest",
    "key": "datacard", "busy": "icon_marrow_refresh",
    "node": "barcode", "crate": "crate", "crate_locked": "hunter", "file": "objects",
    "pallet": "pallet", "manifest": "datacard",
    "empty_crate": "hunter-reload", "empty_pallet": "packed-pallet",
}


class Icons:
    def __init__(self, master):
        self.master = master
        self._base, self._sized = {}, {}

    def _load(self, name):
        if name not in self._base:
            path = os.path.join(ICON_DIR, name + ".png")
            try:
                self._base[name] = tk.PhotoImage(master=self.master, file=path)
            except (tk.TclError, OSError):
                self._base[name] = None
        return self._base[name]

    def get(self, role: str, size: int = 16):
        """PhotoImage for a role at roughly `size` px (integer-scaled), or None."""
        name = ROLES.get(role, role)
        key = (name, size)
        if key not in self._sized:
            base = self._load(name)
            if base is None:
                self._sized[key] = None
            else:
                longest = max(base.width(), base.height())
                if longest >= size:
                    factor = max(1, round(longest / size))
                    self._sized[key] = base.subsample(factor) if factor > 1 else base
                else:
                    factor = max(1, round(size / longest))
                    self._sized[key] = base.zoom(factor) if factor > 1 else base
        return self._sized[key]

    def kw(self, role: str, size: int = 16) -> dict:
        """Keyword args for a ttk widget: image + compound, or nothing."""
        img = self.get(role, size)
        return {"image": img, "compound": "left"} if img is not None else {}

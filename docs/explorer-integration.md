# Windows Explorer integration (icons + right-click menu)

Goal: double-clicking a `.crate`/`.pallet` opens Asset Warehouse, the files get their own icons,
and right-click shows a **File Warehouse ▸** submenu (like PeaZip / 7-Zip):

```
some.crate  ── right-click ──▶  File Warehouse ▸   Extract crate here
                                                   Extract to "some\"
                                                   Open in Asset Warehouse
any file/folder ── right-click ─▶ File Warehouse ▸ Add to "name.crate"
```

None of this needs a shell extension DLL to start with. Everything below is **registry keys under
`HKEY_CURRENT_USER\Software\Classes`**, so no admin rights are needed and uninstalling just means
deleting the keys. There are five pieces, roughly in the order you'd build them.

---

## 1. A command-line entry point (prerequisite)

Explorer can only *run a command* with the clicked file's path. So the app needs a non-GUI
entry point first, e.g. `python -m asset_warehouse <verb> <path>`:

| Command | What it does |
|---|---|
| `AssetWarehouse.exe "C:\x\a.crate"` | open the GUI with that file in the inspector |
| `AssetWarehouse.exe extract-here "C:\x\a.crate"` | unpack next to the file, no main window |
| `AssetWarehouse.exe extract-to "C:\x\a.crate"` | unpack into `C:\x\a\` |
| `AssetWarehouse.exe add-to-crate "C:\x\folder"` | pack into `C:\x\folder.crate` |

Sketch of `asset_warehouse/__main__.py`:

```python
import argparse, os, sys

def main():
    p = argparse.ArgumentParser(prog="AssetWarehouse")
    p.add_argument("verb", nargs="?", default="open")
    p.add_argument("paths", nargs="*")
    args = p.parse_args()
    if args.verb in ("open",) or os.path.exists(args.verb):   # plain double-click
        from .ui import main as gui; gui(open_paths=[args.verb, *args.paths])
    elif args.verb == "extract-here":
        from .shell_actions import extract; extract(args.paths, here=True)
    elif args.verb == "extract-to":
        from .shell_actions import extract; extract(args.paths, here=False)
    elif args.verb == "add-to-crate":
        from .shell_actions import add_to_crate; add_to_crate(args.paths)

if __name__ == "__main__":
    main()
```

The headless actions still show a **small progress window** (a Tk `Toplevel` with the
`Progressbar` and status label we already have, plus the dark theme). If you launch with
`pythonw.exe` or a `--noconsole` build there's no console, so errors also have to appear in a dialog.

### The manifest problem

"Extract here" has to decrypt without anyone opening the Manifests tab. You need a
**persistent keyring**:

- Remember the manifests the user loads, e.g. paths in `%APPDATA%\AssetWarehouse\keyring.json`,
  or keep copies in `%APPDATA%\AssetWarehouse\manifests\`.
- On extract, read the crate's public header (`key_id` and `manifest` name, already there),
  look up a matching manifest, and:
  - **unprotected**: just load it
  - **passphrase-protected**: show the themed passphrase dialog (`dialogs.ask_string(..., show="•")`)
  - **not found**: show "This crate needs manifest *TeamKey* (id 1f86…). Browse…"
- Optional convenience: cache unlocked keys for the session with Windows DPAPI
  (`CryptProtectData`, via `ctypes` or `pywin32`), which encrypts them to the current Windows user.

## 2. Package it as an .exe

Registry commands are much cleaner pointing at an exe than at `python.exe path\to\main.py`.

```bash
pip install pyinstaller
pyinstaller --noconsole --name AssetWarehouse --icon icons/app.ico --add-data "icons;icons" main.py
```

That gives you `dist\AssetWarehouse\AssetWarehouse.exe`. `iconset.py` finds the icons through
`__file__`; in a frozen build use `getattr(sys, "_MEIPASS", <project dir>)` as the base instead.

During development you can skip this and use
`"C:\...\pythonw.exe" "C:\...\FIleWarehouseV3\main.py"` as the command.

## 3. Icons

Windows wants **`.ico`** files with several sizes inside: at least 16, 24, 32, 48 and 256 px.
Your art is pixel art, so scale it up with **nearest-neighbour** to keep it crisp:

```python
from PIL import Image
for name in ("crate", "pallet", "datacard"):
    src = Image.open(f"icons/{name}.png").convert("RGBA")          # 32x32
    big = src.resize((256, 256), Image.NEAREST)                     # 8x, stays sharp
    big.save(f"icons/{name}.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)])
```

(16 px is an exact half and 24 px isn't an integer factor, so for the best small sizes draw
dedicated 16/24 px versions, as you would for any pixel-art icon set.)

Where each icon goes:

| Icon | Used by |
|---|---|
| `app.ico` | the exe (`--icon`), taskbar, and `root.iconbitmap(default="app.ico")` in Tk |
| `crate.ico` | `.crate` files in Explorer (`DefaultIcon` key) |
| `pallet.ico` | `.pallet` files |
| `datacard.ico` | `.manifest` files |
| any of them | the context-menu entries (`Icon` value) |

Showing a *preview* of the contents on the file icon (a thumbnail) needs an `IThumbnailProvider`
COM handler, which is a different, much bigger project. Static per-type icons cover 95% of the look.

## 4. Registry: file types + cascading context menu

Each extension points at a **ProgID**, and the ProgID holds the icon, the double-click action, and the menu.
A **cascading submenu** is a verb with `SubCommands` set to an empty string and its own nested
`shell` key (supported since Windows 7). Submenu items are sorted by key name, hence the `10`, `20` prefixes.

```
HKCU\Software\Classes\.crate                         (Default) = AssetWarehouse.Crate
HKCU\Software\Classes\AssetWarehouse.Crate           (Default) = Asset Warehouse Crate
  DefaultIcon                                        (Default) = C:\...\icons\crate.ico
  shell\open\command                                 (Default) = "C:\...\AssetWarehouse.exe" "%1"
  shell\AssetWarehouse                               MUIVerb = File Warehouse
                                                     SubCommands = ""            ← makes it a submenu
                                                     Icon = C:\...\icons\crate.ico
  shell\AssetWarehouse\shell\10extracthere           MUIVerb = Extract crate here
  shell\AssetWarehouse\shell\10extracthere\command   (Default) = "C:\...\AssetWarehouse.exe" extract-here "%1"
  shell\AssetWarehouse\shell\20extractto             MUIVerb = Extract to folder
  shell\AssetWarehouse\shell\20extractto\command     (Default) = "...exe" extract-to "%1"
  shell\AssetWarehouse\shell\30open                  MUIVerb = Open in Asset Warehouse
                                                     CommandFlags = 0x20 (DWORD) ← separator above
  shell\AssetWarehouse\shell\30open\command          (Default) = "...exe" "%1"

HKCU\Software\Classes\*\shell\AssetWarehouse                 ← every file
HKCU\Software\Classes\Directory\shell\AssetWarehouse         ← every folder
  (same pattern, with an "Add to crate" item → add-to-crate "%1")
HKCU\Software\Classes\Directory\Background\shell\...         ← right-click empty space ("%V" = that folder)
```

Repeat for `.pallet` (`AssetWarehouse.Pallet`, items like "Unpack pallet here") and `.manifest`
(`AssetWarehouse.Manifest`, "Add to keyring").

A small registration script using `winreg` (stdlib), which you'd ship as `register_shell.py`
or run from the installer:

```python
import ctypes, os, sys, winreg

EXE = r'"C:\Program Files\Asset Warehouse\AssetWarehouse.exe"'
ICONS = r"C:\Program Files\Asset Warehouse\icons"

def put(path, values):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Classes" + "\\" + path) as k:
        for name, val in values.items():
            kind = winreg.REG_DWORD if isinstance(val, int) else winreg.REG_SZ
            winreg.SetValueEx(k, name, 0, kind, val)

def menu(base, icon, items):
    put(base + r"\shell\AssetWarehouse", {"MUIVerb": "File Warehouse", "SubCommands": "", "Icon": icon})
    for key, label, args, flags in items:
        item = base + rf"\shell\AssetWarehouse\shell\{key}"
        put(item, {"MUIVerb": label, **({"CommandFlags": flags} if flags else {})})
        put(item + r"\command", {"": f'{EXE} {args}'})

def register():
    for ext, progid, desc, ico, items in [
        (".crate", "AssetWarehouse.Crate", "Asset Warehouse Crate", "crate.ico", [
            ("10extracthere", "Extract crate here", 'extract-here "%1"', 0),
            ("20extractto", "Extract to folder", 'extract-to "%1"', 0),
            ("30open", "Open in Asset Warehouse", '"%1"', 0x20)]),
        (".pallet", "AssetWarehouse.Pallet", "Asset Warehouse Pallet", "pallet.ico", [
            ("10extracthere", "Unpack pallet here", 'extract-here "%1"', 0),
            ("30open", "Open in Asset Warehouse", '"%1"', 0x20)]),
    ]:
        icon = os.path.join(ICONS, ico)
        put(ext, {"": progid})
        put(progid, {"": desc})
        put(progid + r"\DefaultIcon", {"": icon})
        put(progid + r"\shell\open\command", {"": f'{EXE} "%1"'})
        menu(progid, icon, items)
    for target in ("*", "Directory"):
        menu(target, os.path.join(ICONS, "crate.ico"),
             [("10addcrate", "Add to crate", 'add-to-crate "%1"', 0)])
    # tell Explorer file associations changed so icons refresh without a reboot
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
```

Unregistering is the reverse: `winreg.DeleteTree` on every key you created, then the same
`SHChangeNotify` call. Test with the Python `pythonw.exe` command first; switch `EXE` to the
packaged exe later.

### Caveats worth knowing up front

- **Windows 11's new context menu.** Classic registry verbs only appear under
  *Show more options* (or Shift+right-click). Getting into the compact top-level menu needs an
  `IExplorerCommand` COM handler (realistically C++ or C#, not Python) registered through an MSIX
  or *sparse* package with a signing certificate. That's how the big archivers do it. Ship the
  registry version first; add this later if you want it.
- **Multiple selected files.** For a static verb, Explorer launches **one process per selected
  file** (and hides the verb when more than 15 are selected). For "Add these 30 files to one
  crate" you need either:
  - a **single-instance** app: the first process opens a named pipe / local socket, later
    processes send it their path and exit, and after ~300 ms of quiet the first one packs
    everything (simple and common), or
  - a `DropTarget` / `IExplorerCommand` handler that receives the whole selection at once.
  "Send To" shortcuts (`shell:sendto`) also pass every selected path in one go, which makes a handy
  stopgap.
- **Don't steal `.zip`-style defaults.** Only claim your own extensions. For files the user might
  want to open with other apps, add to `OpenWithProgids` instead of replacing the default.

## 5. An installer

[Inno Setup](https://jrsoftware.org/isinfo.php) (free) packages the PyInstaller output and
writes the same registry keys, removing them on uninstall:

```ini
[Registry]
Root: HKCU; Subkey: "Software\Classes\.crate"; ValueType: string; ValueData: "AssetWarehouse.Crate"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\AssetWarehouse.Crate"; ValueType: string; ValueData: "Asset Warehouse Crate"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\AssetWarehouse.Crate\DefaultIcon"; ValueType: string; ValueData: "{app}\icons\crate.ico"
Root: HKCU; Subkey: "Software\Classes\AssetWarehouse.Crate\shell\open\command"; ValueType: string; ValueData: """{app}\AssetWarehouse.exe"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\AssetWarehouse.Crate\shell\AssetWarehouse"; ValueName: "MUIVerb"; ValueType: string; ValueData: "File Warehouse"
Root: HKCU; Subkey: "Software\Classes\AssetWarehouse.Crate\shell\AssetWarehouse"; ValueName: "SubCommands"; ValueType: string; ValueData: ""
; ...one line per value from the table above...

[Setup]
ChangesAssociations=yes   ; makes the installer refresh Explorer icons for you
```

## Suggested order

1. CLI verbs + headless progress window + persistent keyring (all testable from a terminal).
2. `.ico` files + `register_shell.py` pointing at `pythonw main.py`, then try it in Explorer.
3. PyInstaller build, re-point the registry at the exe.
4. Inno Setup installer.
5. (Optional) single-instance multi-select, then the Windows 11 top-level menu handler.

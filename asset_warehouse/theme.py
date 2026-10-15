"""Dark ttk theme built around the neon magenta / cyan of the icon set."""

import ctypes
import sys
import tkinter as tk
from tkinter import ttk

P = {
    "bg":        "#12101a",   # window
    "surface":   "#1a1724",   # fields, lists
    "raised":    "#252034",   # buttons, headings
    "hover":     "#322a48",
    "pressed":   "#3d3358",
    "border":    "#352d4d",
    "fg":        "#ece8f5",
    "muted":     "#9c93b3",
    "dim":       "#5d5675",
    "cyan":      "#00fbff",   # straight from the icons
    "cyan_dim":  "#00c2c5",
    "magenta":   "#ea00ff",
    "magenta_hi": "#f45cff",
    "select":    "#4a1a63",
    "on_accent": "#12101a",
}

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI Semibold", 10)
FONT_TITLE = ("Segoe UI Semibold", 11)


def dark_titlebar(win) -> None:
    """Ask Windows 10/11 to draw this window's title bar dark."""
    if sys.platform != "win32":
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (newer, older builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:  # noqa: BLE001 - purely cosmetic
        pass


def apply(root: tk.Tk) -> ttk.Style:
    s = ttk.Style(root)
    s.theme_use("clam")  # the most colour-configurable built-in theme
    root.configure(bg=P["bg"])

    flat = dict(lightcolor=P["bg"], darkcolor=P["bg"])
    s.configure(".", background=P["bg"], foreground=P["fg"], fieldbackground=P["surface"],
                bordercolor=P["border"], troughcolor=P["surface"], selectbackground=P["select"],
                selectforeground=P["fg"], insertcolor=P["fg"], focuscolor=P["dim"], font=FONT, **flat)
    s.map(".", foreground=[("disabled", P["dim"])])

    s.configure("TFrame", background=P["bg"])
    s.configure("Card.TFrame", background=P["surface"])
    s.configure("TLabel", background=P["bg"], foreground=P["fg"])
    s.configure("Muted.TLabel", foreground=P["muted"])
    s.configure("Section.TLabel", foreground=P["muted"], font=FONT_BOLD)
    s.configure("Path.TLabel", foreground=P["cyan_dim"])
    s.configure("Status.TLabel", background=P["surface"], foreground=P["muted"], padding=(4, 0))
    s.configure("Keys.TLabel", background=P["surface"], foreground=P["cyan"], padding=(4, 0))
    s.configure("Status.TFrame", background=P["surface"])
    s.configure("Empty.TLabel", background=P["surface"], foreground=P["dim"], font=FONT_TITLE)

    s.configure("TLabelframe", background=P["bg"], bordercolor=P["border"],
                lightcolor=P["border"], darkcolor=P["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=P["bg"], foreground=P["cyan"], font=FONT_TITLE)

    # buttons
    btn = dict(background=P["raised"], foreground=P["fg"], bordercolor=P["border"],
               lightcolor=P["raised"], darkcolor=P["raised"], padding=(8, 4), relief="flat",
               focusthickness=1, focuscolor=P["raised"])
    s.configure("TButton", **btn)
    for opt in ("background", "lightcolor", "darkcolor"):
        s.map("TButton", **{opt: [("disabled", P["surface"]), ("pressed", P["pressed"]),
                                  ("active", P["hover"])]})
    s.map("TButton", bordercolor=[("focus", P["cyan_dim"])], foreground=[("disabled", P["dim"])])

    s.configure("Accent.TButton", background=P["magenta"], foreground=P["on_accent"],
                lightcolor=P["magenta"], darkcolor=P["magenta"], bordercolor=P["magenta"],
                font=FONT_BOLD, padding=(12, 6), focuscolor=P["magenta"])
    for opt in ("background", "lightcolor", "darkcolor", "bordercolor"):
        s.map("Accent.TButton", **{opt: [("disabled", P["raised"]), ("pressed", P["magenta"]),
                                         ("active", P["magenta_hi"])]})
    s.map("Accent.TButton", foreground=[("disabled", P["dim"])])

    # text inputs
    field = dict(fieldbackground=P["surface"], foreground=P["fg"], bordercolor=P["border"],
                 lightcolor=P["surface"], darkcolor=P["surface"], insertcolor=P["cyan"], padding=5)
    s.configure("TEntry", **field)
    s.map("TEntry", bordercolor=[("focus", P["cyan_dim"])], lightcolor=[("focus", P["surface"])])
    s.configure("TCombobox", background=P["raised"], arrowcolor=P["cyan"], arrowsize=14, **field)
    s.map("TCombobox",
          fieldbackground=[("readonly", P["surface"])],
          selectbackground=[("readonly", P["surface"])],
          selectforeground=[("readonly", P["fg"])],
          background=[("active", P["hover"]), ("pressed", P["pressed"])],
          bordercolor=[("focus", P["cyan_dim"])], arrowcolor=[("disabled", P["dim"])])
    # the combobox dropdown is a classic Tk listbox
    root.option_add("*TCombobox*Listbox.background", P["surface"])
    root.option_add("*TCombobox*Listbox.foreground", P["fg"])
    root.option_add("*TCombobox*Listbox.selectBackground", P["select"])
    root.option_add("*TCombobox*Listbox.selectForeground", P["fg"])
    root.option_add("*TCombobox*Listbox.font", FONT)
    root.option_add("*TCombobox*Listbox.borderWidth", 0)

    s.configure("TCheckbutton", background=P["bg"], foreground=P["fg"],
                indicatorbackground=P["surface"], indicatorforeground=P["on_accent"],
                upperbordercolor=P["border"], lowerbordercolor=P["border"], focuscolor=P["bg"])
    s.map("TCheckbutton",
          indicatorbackground=[("selected", P["cyan"]), ("active", P["hover"])],
          background=[("active", P["bg"])])

    # lists / trees
    s.configure("Treeview", background=P["surface"], fieldbackground=P["surface"],
                foreground=P["fg"], bordercolor=P["border"], lightcolor=P["surface"],
                darkcolor=P["surface"], rowheight=26)
    s.map("Treeview", background=[("selected", P["select"])], foreground=[("selected", P["fg"])])
    s.configure("Treeview.Heading", background=P["raised"], foreground=P["muted"],
                bordercolor=P["border"], lightcolor=P["raised"], darkcolor=P["raised"],
                relief="flat", padding=(6, 4), font=FONT_BOLD)
    s.map("Treeview.Heading", background=[("active", P["hover"])])
    s.configure("Treeview.Item", indicatorsize=12)

    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background=P["raised"], troughcolor=P["surface"],
                    bordercolor=P["surface"], lightcolor=P["raised"], darkcolor=P["raised"],
                    arrowcolor=P["muted"], arrowsize=13, gripcount=0)
        s.map(f"{orient}.TScrollbar", background=[("pressed", P["pressed"]), ("active", P["hover"])],
              arrowcolor=[("active", P["cyan"])])

    # notebook
    s.configure("TNotebook", background=P["bg"], bordercolor=P["border"], tabmargins=(6, 6, 6, 0),
                lightcolor=P["bg"], darkcolor=P["bg"])
    s.configure("TNotebook.Tab", background=P["bg"], foreground=P["muted"], bordercolor=P["bg"],
                lightcolor=P["bg"], darkcolor=P["bg"], padding=(14, 7), font=FONT_BOLD)
    s.map("TNotebook.Tab",
          background=[("selected", P["raised"]), ("active", P["surface"])],
          foreground=[("selected", P["cyan"]), ("active", P["fg"])],
          lightcolor=[("selected", P["magenta"])],  # clam draws this on the tab's top edge
          bordercolor=[("selected", P["border"])])

    s.configure("TProgressbar", background=P["magenta"], troughcolor=P["bg"],
                bordercolor=P["border"], lightcolor=P["magenta"], darkcolor=P["magenta"], thickness=10)
    s.configure("TPanedwindow", background=P["bg"])
    s.configure("Sash", sashthickness=8, gripcount=0, background=P["bg"],
                lightcolor=P["bg"], darkcolor=P["bg"], bordercolor=P["bg"])
    s.configure("TSeparator", background=P["border"])
    return s

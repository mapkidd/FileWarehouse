"""Themed replacements for tkinter.messagebox / simpledialog.

The stock Windows message boxes are native Win32 dialogs and always light,
which looks jarring in a dark app. File pickers stay native (they're far more
capable than anything Tk can draw).
"""

import tkinter as tk
from tkinter import ttk

from . import theme

_GLYPHS = {  # kind -> (glyph, colour)
    "info": ("i", theme.P["cyan"]),
    "warning": ("!", "#ffcc33"),
    "error": ("×", "#ff4d6d"),
    "question": ("?", theme.P["magenta"]),
}


class _Dialog(tk.Toplevel):
    def __init__(self, parent, title, message, kind="info", buttons=(("OK", True),),
                 entry=None, show=None, icon=None):
        parent = parent.winfo_toplevel()
        super().__init__(parent)
        self.withdraw()
        self.title(title)
        self.configure(bg=theme.P["bg"])
        self.transient(parent)
        self.resizable(False, False)
        self.result = None

        body = ttk.Frame(self, padding=(20, 18, 20, 12))
        body.pack(fill="both", expand=True)
        if icon is not None:
            ttk.Label(body, image=icon).grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 16))
        else:
            glyph, colour = _GLYPHS.get(kind, _GLYPHS["info"])
            badge = tk.Canvas(body, width=36, height=36, bg=theme.P["bg"], highlightthickness=0)
            badge.create_oval(2, 2, 34, 34, outline=colour, width=2)
            badge.create_text(18, 18, text=glyph, fill=colour, font=("Segoe UI Semibold", 15))
            badge.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 16))
        ttk.Label(body, text=message, wraplength=440, justify="left").grid(row=0, column=1, sticky="w")

        self.var = None
        if entry is not None:
            self.var = tk.StringVar(value=entry)
            ent = ttk.Entry(body, textvariable=self.var, width=46, show=show or "")
            ent.grid(row=1, column=1, sticky="ew", pady=(10, 0))
            ent.select_range(0, "end")
            ent.icursor("end")
            self.after(50, ent.focus_set)

        bar = ttk.Frame(self, padding=(20, 0, 20, 16))
        bar.pack(fill="x")
        default_value = buttons[0][1]
        for i, (label, value) in enumerate(reversed(buttons)):
            is_default = value == default_value
            b = ttk.Button(bar, text=label, style="Accent.TButton" if is_default else "TButton",
                           command=lambda v=value: self._finish(v))
            b.pack(side="right", padx=(8, 0))
            if is_default and entry is None:
                self.after(50, b.focus_set)

        cancel_value = buttons[-1][1] if len(buttons) > 1 else default_value
        self.bind("<Return>", lambda e: self._finish(default_value))
        self.bind("<Escape>", lambda e: self._finish(cancel_value))
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(cancel_value))

        # centre over the parent, then show
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        theme.dark_titlebar(self)
        self.deiconify()
        self.grab_set()
        self.wait_window()

    def _finish(self, value):
        if self.var is not None and value is True:
            self.result = self.var.get()
        elif self.var is None:
            self.result = value
        self.grab_release()
        self.destroy()


def info(parent, title, message, icon=None):
    _Dialog(parent, title, message, "info", icon=icon)


def warning(parent, title, message):
    _Dialog(parent, title, message, "warning")


def error(parent, title, message):
    _Dialog(parent, title, message, "error")


def ask_yes_no(parent, title, message) -> bool:
    return bool(_Dialog(parent, title, message, "question",
                        buttons=(("Yes", True), ("No", False))).result)


def ask_string(parent, title, prompt, initial="", show=None):
    """Returns the entered text, or None if cancelled."""
    return _Dialog(parent, title, prompt, "question", buttons=(("OK", True), ("Cancel", False)),
                   entry=initial, show=show).result

import tkinter as tk
from tkinter import ttk, filedialog
from Forklift import pack_files, extract_files, pack_pallet, extract_pallet, list_pallet_contents

# ---------------- Main window ----------------
root = tk.Tk()
root.title("Asset Warehouse")
root.configure(bg='#2b2b2b')

icon_path = "icons/window_icon.ico"
root.iconbitmap(icon_path)

# ---------------- Styles ----------------
style = ttk.Style()
style.theme_use("default")
style.configure("Treeview",
                background="#2b2b2b",
                foreground="white",
                fieldbackground="#2b2b2b",
                rowheight=22)
style.configure("Treeview.Heading",
                background="#3a3a3a",
                foreground="white")
style.map("Treeview",
          background=[("selected", "#444444")],
          foreground=[("selected", "white")])

# ---------------- Load button icons ----------------
pack_icon = tk.PhotoImage(file="icons/pack_icon.png")   # use .png for transparency
unpack_icon = tk.PhotoImage(file="icons/extract_icon.png")
pallet_icon = tk.PhotoImage(file="icons/pallet_icon.png")

# ---------------- Crates ----------------
def pack_crate():
    file_paths = filedialog.askopenfilenames(title="Select files for crate")
    if not file_paths: return
    cra_file = filedialog.asksaveasfilename(defaultextension=".cra",
                                            filetypes=[("Crates", "*.cra")])
    if not cra_file: return
    pack_files(file_paths, cra_file)

def unpack_crate():
    cra_file = filedialog.askopenfilename(filetypes=[("Crates", "*.cra")])
    if not cra_file: return
    folder = filedialog.askdirectory()
    if not folder: return
    extract_files(cra_file, folder)

# ---------------- Pallets ----------------
def open_pallet_manager():
    pal_win = tk.Toplevel(root)
    pal_win.title("Pallet Manager")
    pal_win.configure(bg='#2b2b2b')
    pal_win.iconbitmap(icon_path)

    # Treeview
    tree = ttk.Treeview(pal_win)
    tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    tree.heading("#0", text="Pallet Contents")

    def load_pallet():
        pal_file = filedialog.askopenfilename(filetypes=[("Pallets", "*.pal")])
        if not pal_file: return
        tree.delete(*tree.get_children())
        for crate in list_pallet_contents(pal_file):
            tree.insert("", "end", text=crate)

    def pack_pal():
        crates = filedialog.askopenfilenames(filetypes=[("Crates", "*.cra")],
                                             title="Select crates for pallet")
        if not crates: return
        pal_file = filedialog.asksaveasfilename(defaultextension=".pal",
                                                filetypes=[("Pallets", "*.pal")])
        if not pal_file: return
        pack_pallet(crates, pal_file)
        load_pallet()

    def unpack_pal():
        pal_file = filedialog.askopenfilename(filetypes=[("Pallets", "*.pal")])
        if not pal_file: return
        folder = filedialog.askdirectory()
        if not folder: return
        extract_pallet(pal_file, folder)

    # Buttons with icons
    btn_frame = tk.Frame(pal_win, bg='#2b2b2b')
    btn_frame.pack(fill=tk.X, pady=5)

    tk.Button(btn_frame, text=" Open Pallet", command=load_pallet,
              bg="#444444", fg="white", image=pallet_icon,
              compound="left").pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text=" Pack Pallet", command=pack_pal,
              bg="#444444", fg="white", image=pack_icon,
              compound="left").pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text=" Unpack Pallet", command=unpack_pal,
              bg="#444444", fg="white", image=unpack_icon,
              compound="left").pack(side=tk.LEFT, padx=5)

# ---------------- Main UI ----------------
tk.Button(root, text=" Pack Crate", command=pack_crate,
          bg="#444444", fg="white", image=pack_icon,
          compound="left").pack(pady=5)

tk.Button(root, text=" Unpack Crate", command=unpack_crate,
          bg="#444444", fg="white", image=unpack_icon,
          compound="left").pack(pady=5)

tk.Button(root, text=" Pallet Manager", command=open_pallet_manager,
          bg="#444444", fg="white", image=pallet_icon,
          compound="left").pack(pady=5)

tk.Label(root, text="Forklift Brand: Standard (.cra / .pal)",
         bg="#2b2b2b", fg="white").pack(pady=10)

root.mainloop()

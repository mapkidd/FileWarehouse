from Forklift import pack_to_cra, extract_from_cra
import tkinter as tk
from tkinter import filedialog

def craeate_gui():
    def pack_files():
        file_paths = filedialog.askopenfilenames()
        cra_filename = filedialog.asksaveasfilename(defaultextension=".cra", filetypes=[("crate files", "*.cra")])
        if file_paths and cra_filename:
            pack_to_cra(file_paths, cra_filename)
            print(f"Packed files into {cra_filename}")

    def extract_files():
        cra_filename = filedialog.askopenfilename(filetypes=[("crate files", "*.cra")])
        extract_path = filedialog.askdirectory()
        if cra_filename and extract_path:
            extract_from_cra(cra_filename, extract_path)
            print(f"Extracted files to {extract_path}")

    # Dark theme colors
    bg_color = "#2e2e2e"  # Background color
    fg_color = "#ffffff"  # Foreground (text) color
    btn_bg_color = "#444444"  # Button background color
    btn_fg_color = "#ffffff"  # Button text color

    root = tk.Tk()
    root.title("File Warehouse")

    # Set the custom window icon
    root.iconbitmap('icons/window_icon.ico')

    # Set the overall theme of the root window
    root.configure(bg=bg_color)

    # Load the icon images
    pack_icon = tk.PhotoImage(file="icons/pack_icon.png")
    extract_icon = tk.PhotoImage(file="icons/extract_icon.png")

    # craeate styled buttons with icons
    pack_button = tk.Button(root, text="Pack Files", command=pack_files, image=pack_icon, compound=tk.LEFT,
                            bg=btn_bg_color, fg=btn_fg_color, activebackground=btn_bg_color, activeforeground=btn_fg_color)
    pack_button.pack(pady=12)
    pack_button.pack(padx=8)

    extract_button = tk.Button(root, text="Extract Files", command=extract_files, image=extract_icon, compound=tk.LEFT,
                               bg=btn_bg_color, fg=btn_fg_color, activebackground=btn_bg_color, activeforeground=btn_fg_color)
    extract_button.pack(pady=12)
    extract_button.pack(padx=8)

    forklift_info = tk.Label(root, text="Forklift Brand: Standard (.cra)", bg=bg_color, fg=fg_color)
    forklift_info.pack(side=tk.BOTTOM, pady=16)
    forklift_info.pack(padx=8)

    # Configure text and button styles
    style = {
        'bg': bg_color,
        'fg': fg_color,
        'activebackground': btn_bg_color,
        'activeforeground': btn_fg_color
    }

    root.option_add("*Button.Background", btn_bg_color)
    root.option_add("*Button.Foreground", btn_fg_color)
    root.option_add("*Button.ActiveBackground", btn_bg_color)
    root.option_add("*Button.ActiveForeground", btn_fg_color)

    root.mainloop()

if __name__ == "__main__":
    craeate_gui()

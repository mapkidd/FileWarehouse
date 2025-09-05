import os
import zipfile

# ---------------- Crates ----------------
def pack_files(file_paths, output_file, icon_path=None):
    """Pack normal files into a .cra crate"""
    with zipfile.ZipFile(output_file, 'w') as zf:
        for file in file_paths:
            zf.write(file, os.path.basename(file))

def extract_files(cra_file, output_folder):
    """Extract .cra crate into folder"""
    try:
        with zipfile.ZipFile(cra_file, 'r') as zf:
            zf.extractall(output_folder)
    except Exception as e:
        print(f"Failed to extract files: {e}")

# ---------------- Pallets ----------------
def pack_pallet(crate_files, pallet_file):
    """Pack multiple .cra crate files into a .pal pallet"""
    with zipfile.ZipFile(pallet_file, 'w') as zf:
        for crate in crate_files:
            zf.write(crate, os.path.basename(crate))

def extract_pallet(pallet_file, output_folder):
    """Extract all .cra crates from a .pal pallet"""
    try:
        with zipfile.ZipFile(pallet_file, 'r') as zf:
            zf.extractall(output_folder)
    except Exception as e:
        print(f"Failed to extract pallet: {e}")

def list_pallet_contents(pallet_file):
    """Return list of .cra files inside a pallet"""
    try:
        with zipfile.ZipFile(pallet_file, 'r') as zf:
            return zf.namelist()
    except Exception as e:
        print(f"Failed to read pallet: {e}")
        return []

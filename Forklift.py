# Forklift.py

import os
import struct
import platform

def pack_to_cra(files, cra_filename):
    with open(cra_filename, 'wb') as cra_file:
        num_files = len(files)
        cra_file.write(struct.pack('<I', num_files))  # Write number of files as a 4-byte integer

        for file_path in files:
            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            file_attributes = os.stat(file_path)  # Get file attributes

            # Write file metadata (filename, size, and attributes)
            cra_file.write(struct.pack('<128sQ', filename.encode(), file_size))
            cra_file.write(struct.pack('<III', file_attributes.st_mode, file_attributes.st_uid, file_attributes.st_gid))
            
            # Write file content
            with open(file_path, 'rb') as f:
                cra_file.write(f.read())

def extract_from_cra(cra_filename, extract_path):
    with open(cra_filename, 'rb') as cra_file:
        num_files = struct.unpack('<I', cra_file.read(4))[0]  # Read number of files
        
        for _ in range(num_files):
            filename, file_size = struct.unpack('<128sQ', cra_file.read(136))  # Read filename and size
            filename = filename.decode().strip('\x00')  # Convert bytes to string and strip null bytes
            
            # Read file attributes
            file_mode, file_uid, file_gid = struct.unpack('<III', cra_file.read(12))

            # Read file content
            file_content = cra_file.read(file_size)
            
            # Write file content to disk
            file_path = os.path.join(extract_path, filename)
            with open(file_path, 'wb') as f:
                f.write(file_content)

            # Set file attributes if supported
            if platform.system() != 'Windows':
                try:
                    os.chmod(file_path, file_mode)
                    os.chown(file_path, file_uid, file_gid)
                except AttributeError:
                    print(f"Warning: Setting file attributes (mode, uid, gid) for '{filename}' not supported on this platform.")
                except OSError as e:
                    print(f"Error setting file attributes (mode, uid, gid) for '{filename}': {str(e)}")
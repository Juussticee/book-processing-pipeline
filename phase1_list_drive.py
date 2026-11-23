"""
Phase 1: List CURRENT contents of Google Drive folder.
Only lists, downloads, and reports image metadata. No production code modification.
"""

import os
import sys

sys.path.insert(0, "C:\\Users\\hashe\\Documents\\Book Auth")
from PIL import Image
from PIL.ExifTags import TAGS

from core.config import Config
from services.drive_service import DriveService

config = Config.from_env()
drive = DriveService(config)
drive.authenticate()

files, _ = drive.list_files()
print(f"Total files found in Drive: {len(files)}")
print()

download_dir = "data/diagnostic_downloads"
os.makedirs(download_dir, exist_ok=True)

for i, f in enumerate(files):
    file_id = f.file_id
    filename = f.name
    size = f.size

    local_path = os.path.join(download_dir, f"{file_id}.jpg")
    drive.download_file(f, local_path)

    img = Image.open(local_path)
    w, h = img.size

    exif_orient = None
    exif_data = img._getexif() if hasattr(img, "_getexif") else None
    if exif_data:
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "Orientation":
                exif_orient = value

    print(f"{i+1}. ID={file_id}")
    print(f"   Filename: {filename}")
    print(f"   Size: {size} bytes")
    print(f"   Dimensions: {w}x{h}")
    print(f"   EXIF orientation: {exif_orient}")
    print(f"   Local path: {local_path}")
    print()

"""Drive file model."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DriveFile:
    """Represents a file from Google Drive."""

    file_id: str
    name: str
    mime_type: str
    modified_time: str
    size: Optional[int] = None
    md5_checksum: Optional[str] = None
    web_view_link: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.file_id:
            raise ValueError("file_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")

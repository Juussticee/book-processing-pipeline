"""Processing job model with status enum."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    """Possible states for a processing job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"


class ErrorCategory(str, Enum):
    """Lightweight classification for job failures."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class ProcessingJob:
    """Represents a processing job for a downloaded Drive file.

    The unique identifier is ``file_id`` -- the Google Drive file ID.
    A job is created at most once per Drive file.

    Fields:
        job_id: Unique application-level job identifier (UUID).
        file_id: The Google Drive file ID (permanent identity).
        file_name: The file name from Drive.
        local_path: Local filesystem path to the downloaded file.
        status: Current job status (JobStatus enum or string).
        retry_count: Number of failed processing attempts.
        priority: Job priority (higher = more important). Default 0.
        created_at: When the job was created.
        started_at: When processing started.
        completed_at: When processing completed or failed.
        error_message: Error details if processing failed.
        worker_name: Name of the worker that processed this job.
        error_category: Classification of the last failure.
        next_retry_at: ISO timestamp when this job is eligible for retry.
        last_error_at: When the last error occurred.
    """

    job_id: str = ""
    file_id: str = ""
    file_name: str = ""
    local_path: str = ""
    status: JobStatus = JobStatus.PENDING
    retry_count: int = 0
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    worker_name: Optional[str] = None
    error_category: Optional[ErrorCategory] = None
    next_retry_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id cannot be empty")
        if not self.file_id:
            raise ValueError("file_id cannot be empty")
        # Coerce string status to JobStatus enum
        if isinstance(self.status, str):
            object.__setattr__(self, "status", JobStatus(self.status))
        # Coerce string error_category to ErrorCategory enum
        if isinstance(self.error_category, str):
            object.__setattr__(self, "error_category", ErrorCategory(self.error_category))

    @property
    def local_file_path(self) -> str:
        """Alias for local_path (spec field name)."""
        return self.local_path

    @property
    def drive_file_id(self) -> str:
        """The Google Drive file ID (alias for file_id)."""
        return self.file_id

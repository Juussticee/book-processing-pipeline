"""Shared pytest fixtures and configuration.

Sets up environment variables needed by app.py subprocess tests so they
can run without a real .env file.

It also defines where the *real photograph* fixtures live. Those images are
photographs of the owner's own book collection taken from the Drive cache, so
they are deliberately **not distributed with the repository**: a fresh clone
contains no collection images, and publishing them would publish somebody's
private library.

The affected test modules call :func:`require_real_photo_fixtures` at import
time. When the local photo set is present they run exactly as before; when it
is absent they are reported as skipped with the reason below, rather than
failing on a missing file. Set ``REAL_PHOTO_FIXTURES`` to a directory that
holds ``metadata.json`` plus the JPEGs to run them.
"""

import os
from pathlib import Path

import pytest

# Ensure required config is available for subprocess tests that run app.py
os.environ.setdefault("GOOGLE_DRIVE_FOLDER_ID", "test_folder_id")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
os.environ.setdefault("DATA_DIRECTORY", "data")
os.environ.setdefault("TEMP_DIRECTORY", "data/temp")
os.environ.setdefault("LOG_DIRECTORY", "logs")

# Default location of the non-distributed real-photograph fixtures.
REAL_FIXTURE_DIR = Path(
    os.getenv(
        "REAL_PHOTO_FIXTURES",
        str(Path(__file__).resolve().parent.parent / "data" / "fixtures" / "real"),
    )
)

REAL_FIXTURE_MANIFEST = REAL_FIXTURE_DIR / "metadata.json"


def require_real_photo_fixtures() -> None:
    """Skip the calling module when the local photograph set is unavailable."""
    if REAL_FIXTURE_MANIFEST.is_file():
        return
    pytest.skip(
        "real-photograph fixtures are not distributed with this repository "
        "(they are photographs of the owner's collection); set "
        "REAL_PHOTO_FIXTURES to a directory holding metadata.json and the "
        f"JPEGs - looked in {REAL_FIXTURE_DIR}",
        allow_module_level=True,
    )

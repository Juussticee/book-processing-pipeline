"""Unit tests for app.py safe_local_filename helper."""

from app import safe_local_filename


class TestSafeLocalFilename:
    """Tests for safe filename generation from Drive file IDs."""

    def test_basic_filename(self):
        """Drive file ID + normal extension."""
        result = safe_local_filename("1AbCdEfGh", "book.jpg")
        assert result == "1AbCdEfGh.jpg"

    def test_path_traversal_prevented(self):
        """Path traversal in filename is blocked."""
        result = safe_local_filename("file001", "../../../etc/passwd")
        # Extension not valid (more than 4 chars, contains dots/slashes)
        assert result == "file001"

    def test_no_extension(self):
        """File without extension returns just the ID."""
        result = safe_local_filename("file001", "noextension")
        assert result == "file001"

    def test_empty_filename(self):
        """Empty original name returns just the sanitized ID."""
        result = safe_local_filename("file002", "")
        assert result == "file002"

    def test_reserved_chars_in_drive_id(self):
        """Reserved characters in Drive file ID are stripped."""
        result = safe_local_filename("file/../003", "test.jpg")
        assert ".." not in result
        assert "/" not in result

    def test_uppercase_extension_preserved(self):
        """Extension is normalized to lowercase."""
        result = safe_local_filename("file004", "test.PNG")
        assert result.endswith(".png")

    def test_long_extension_rejected(self):
        """Extensions longer than 4 chars are rejected."""
        result = safe_local_filename("file005", "test.txtxx")
        assert result == "file005"

    def test_invalid_extension_rejected(self):
        """Non-alphanumeric extensions are rejected."""
        result = safe_local_filename("file006", "test.jp!g")
        assert result == "file006"

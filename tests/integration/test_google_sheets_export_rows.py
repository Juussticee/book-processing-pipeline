"""Focused tests for Google Sheets row/column writing behavior.

Verifies that exported books occupy sequential rows with correct column alignment,
no diagonal/shifted writes, no duplicate headers, and no duplicate rows.

All tests use a FakeSheet mock — NO real Google Sheets credentials are used.
"""

from __future__ import annotations

from unittest.mock import patch

from models.book_metadata import BookMetadata
from services.google_sheets_service import GoogleSheetsService


def _make_metadata(
    title="Book",
    authors=None,
    publisher=None,
    confidence=0.95,
    drive_file_id="drive001",
    **kwargs,
):
    defaults = {
        "title": title,
        "authors": authors or ["Author"],
        "isbn_10": "",
        "isbn_13": "",
        "publisher": publisher,
        "published_date": "",
        "language": "",
        "categories": [],
        "description": "",
        "cover_url": "",
        "confidence": confidence,
        "source": "google_books",
        "drive_file_id": drive_file_id,
        "job_id": "job001",
    }
    defaults.update(kwargs)
    return BookMetadata(**defaults)


class FakeSheet:
    """Simulates a Google Sheets worksheet with realistic row/column tracking.

    Tracks every write operation and maintains an internal grid state
    so we can verify row and column alignment after each export.
    """

    def __init__(self, columns):
        self.columns = columns
        self.num_cols = len(columns)
        # Internal grid: list of rows, each row is a list of cell values
        self.grid: list[list[str]] = []
        # Track the row that append_rows found (simulating OVERWRITE behavior)
        self._append_calls = []

    def get_all_values(self):
        """Return all rows, padding shorter rows to num_cols."""
        if not self.grid:
            return []
        max_cols = max(len(r) for r in self.grid)
        padded = [list(r) + [""] * (max_cols - len(r)) for r in self.grid]
        return padded

    def append_rows(
        self,
        values,
        value_input_option=None,
        insert_data_option=None,
        include_values_in_response=None,
    ):
        """Simulate append_rows: append each inner list as a new row."""
        self._append_calls.append(
            {
                "values": values,
                "insert_data_option": insert_data_option,
            }
        )
        for row in values:
            self.grid.append(list(row))
        return {"spreadsheetId": "fake", "updates": {"updatedRows": len(values)}}

    def update(
        self, values=None, range_name=None, raw=True, major_dimension=None, **kwargs
    ):
        """Simulate update: write values into the specified range.

        Handles both the new gspread 6.x API (values=, range_name=) and
        the old style (range_name, values).
        """
        if isinstance(range_name, (list, tuple)) and isinstance(values, str):
            range_name, values = values, range_name

        self._append_calls.append(
            {
                "values": values,
                "range_name": range_name,
            }
        )
        if range_name is None or values is None:
            return
        # Parse range like "A2:P2"
        import re

        match = re.match(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", range_name)
        if match:
            start_col = self._col_letter_to_num(match.group(1))
            start_row = int(match.group(2))
            for r_idx, row_data in enumerate(values):
                row_num = start_row + r_idx
                # Find or extend grid
                while len(self.grid) < row_num:
                    self.grid.append([""] * self.num_cols)
                grid_row = self.grid[row_num - 1]
                for c_idx, val in enumerate(row_data):
                    col_num = start_col + c_idx
                    while len(grid_row) < col_num:
                        grid_row.append("")
                    if col_num - 1 < len(grid_row):
                        grid_row[col_num - 1] = val

    @staticmethod
    def _col_letter_to_num(letter):
        num = 0
        for ch in letter:
            num = num * 26 + (ord(ch) - ord("A") + 1)
        return num


class TestSheetsExportRows:
    """Tests that verify correct row/column alignment in Google Sheets export."""

    def setup_method(self):
        self.service = GoogleSheetsService(
            credentials_file="test_creds.json",
            sheets_id="test_sheet_id_123",
            tab_name="Sheet1",
        )
        self.sheets = []  # Track created FakeSheets

    def _patch_get_sheet(self, fake_sheet):
        """Patch _get_sheet to return our fake sheet."""
        return patch.object(self.service, "_get_sheet", return_value=fake_sheet)

    def test_export_one_book(self):
        """Export one book → exactly one data row at row 2."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            row_id = self.service.get_or_create_row(
                _make_metadata(title="Book 1", drive_file_id="d001"), "/cover.jpg"
            )
        values = sheet.get_all_values()
        # Header (row 1) + 1 data row = 2 rows total
        assert len(values) == 2
        assert values[0] == self.service.COLUMNS  # header
        assert (
            values[1][self.service.COLUMNS.index("Title")] == "Book 1"
        )  # title in its canonical column
        assert values[1][self.service.COLUMNS.index("Authors")] == "Author"
        assert row_id == 2

    def test_export_two_books_sequential_rows(self):
        """Export two books → sequential rows A2 and A3."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            self.service.get_or_create_row(
                _make_metadata(title="Book 1", drive_file_id="d001"), "/c1.jpg"
            )
            row_id_2 = self.service.get_or_create_row(
                _make_metadata(title="Book 2", drive_file_id="d002"), "/c2.jpg"
            )
        values = sheet.get_all_values()
        assert len(values) == 3  # header + 2 data rows
        assert values[1][self.service.COLUMNS.index("Title")] == "Book 1"
        assert values[2][self.service.COLUMNS.index("Title")] == "Book 2"
        assert row_id_2 == 3

    def test_export_three_books_all_sequential(self):
        """Export three books → all in sequential rows with correct columns."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            for i in range(1, 4):
                self.service.get_or_create_row(
                    _make_metadata(
                        title=f"Book {i}",
                        authors=[f"Author {i}"],
                        drive_file_id=f"d00{i}",
                    ),
                    f"/c{i}.jpg",
                )
        values = sheet.get_all_values()
        assert len(values) == 4  # header + 3 data
        for i in range(1, 4):
            assert (
                values[i][self.service.COLUMNS.index("Title")] == f"Book {i}"
            ), f"Row {i+1} title column should be Book {i}"
            assert (
                values[i][self.service.COLUMNS.index("Authors")] == f"Author {i}"
            ), f"Row {i+1} authors column should be Author {i}"

    def test_column_alignment_preserved(self):
        """Verify title→A, author→B, publisher→D, isbn13→C consistently."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            self.service.get_or_create_row(
                _make_metadata(
                    title="La violence de l'esprit",
                    authors=["Marcel Poncet"],
                    publisher="Gallimard",
                    isbn_13="9781234567890",
                ),
                "/cover.jpg",
            )
        values = sheet.get_all_values()
        row = values[1]  # data row
        col_map = {name: idx for idx, name in enumerate(self.service.COLUMNS)}
        assert row[col_map["Title"]] == "La violence de l'esprit"
        assert row[col_map["Authors"]] == "Marcel Poncet"
        assert row[col_map["Publisher"]] == "Gallimard"
        assert row[col_map["ISBN-13"]] == "9781234567890"

    def test_publisher_null_row_still_aligned(self):
        """Publisher = null → row still correctly aligned (empty cell)."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            self.service.get_or_create_row(
                _make_metadata(
                    title="No Publisher Book", publisher=None, drive_file_id="d001"
                ),
                "/cover.jpg",
            )
        values = sheet.get_all_values()
        row = values[1]
        col_map = {name: idx for idx, name in enumerate(self.service.COLUMNS)}
        assert row[col_map["Title"]] == "No Publisher Book"
        assert row[col_map["Publisher"]] == ""  # null → empty string

    def test_author_commas_and_accents(self):
        """Author with commas/accents → still one cell."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            self.service.get_or_create_row(
                _make_metadata(
                    title="Test Book",
                    authors=["Doe, Jean-Pierre", "М. Горшков"],
                    drive_file_id="d001",
                ),
                "/cover.jpg",
            )
        values = sheet.get_all_values()
        row = values[1]
        col_map = {name: idx for idx, name in enumerate(self.service.COLUMNS)}
        assert row[col_map["Authors"]] == "Doe, Jean-Pierre, М. Горшков"

    def test_title_commas_accents_apostrophes(self):
        """Title with commas/accents/apostrophes → still one cell."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            self.service.get_or_create_row(
                _make_metadata(
                    title="L'individu et la communauté: essai",
                    authors=["Théodore Litt"],
                    drive_file_id="d001",
                ),
                "/cover.jpg",
            )
        values = sheet.get_all_values()
        row = values[1]
        col_map = {name: idx for idx, name in enumerate(self.service.COLUMNS)}
        assert row[col_map["Title"]] == "L'individu et la communauté: essai"
        assert row[col_map["Authors"]] == "Théodore Litt"

    def test_retry_already_exported_no_duplicate(self):
        """Retry an already-exported book → no duplicate row."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            # First export
            self.service.get_or_create_row(
                _make_metadata(title="Book 1", drive_file_id="d001"), "/c1.jpg"
            )
            # Second export with same title+author → should update, not append
            row_id = self.service.get_or_create_row(
                _make_metadata(
                    title="Book 1", authors=["Author"], drive_file_id="d001"
                ),
                "/c1.jpg",
            )
        values = sheet.get_all_values()
        assert len(values) == 2  # header + 1 data row (no duplicate)
        assert (
            values[1][self.service.COLUMNS.index("Title")] == "Book 1"
        )  # title unchanged
        assert row_id == 2  # same row

    def test_multiple_books_one_batch_sequential_rows(self):
        """Multiple books in one batch → sequential rows, never diagonal."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            for i in range(1, 11):
                self.service.get_or_create_row(
                    _make_metadata(title=f"Book {i}", drive_file_id=f"d{i:03d}"),
                    f"/c{i}.jpg",
                )
        values = sheet.get_all_values()
        assert len(values) == 11  # header + 10 data rows
        for i in range(1, 11):
            assert (
                values[i][self.service.COLUMNS.index("Title")] == f"Book {i}"
            ), f"Row {i+1} should have Book {i} in the title column"
            assert (
                values[i][self.service.COLUMNS.index("Authors")] == "Author"
            ), f"Row {i+1} should have Author in the authors column"

    def test_empty_sheet_first_book_at_row_2(self):
        """Empty sheet → first book starts at row 2 (after header)."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            row_id = self.service.get_or_create_row(
                _make_metadata(title="First Book", drive_file_id="d001"), "/cover.jpg"
            )
        values = sheet.get_all_values()
        assert len(values) == 2  # header + 1 data
        assert values[0] == self.service.COLUMNS
        assert values[1][self.service.COLUMNS.index("Title")] == "First Book"
        assert row_id == 2

    def test_no_duplicate_header_on_second_book(self):
        """Sheet with header only → second book should NOT write a second header."""
        sheet = FakeSheet(self.service.COLUMNS)
        # Pre-populate with header only
        sheet.grid = [list(self.service.COLUMNS)]
        with self._patch_get_sheet(sheet):
            row_id = self.service.get_or_create_row(
                _make_metadata(title="Only Book", drive_file_id="d001"), "/cover.jpg"
            )
        values = sheet.get_all_values()
        assert len(values) == 2  # header + 1 data (NO duplicate header)
        assert values[0] == self.service.COLUMNS
        assert values[1][self.service.COLUMNS.index("Title")] == "Only Book"
        assert row_id == 2

    def test_append_rows_receives_correct_shape(self):
        """Verify append_rows receives a proper 2D array for one row."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            self.service.get_or_create_row(_make_metadata(title="Book 1"), "/c1.jpg")
        # Check the append_rows call shape
        append_call = sheet._append_calls[0]  # First call (append_rows)
        values = append_call["values"]
        # Should be a list of rows: [[header], [row_data]]
        assert len(values) == 2  # header + data
        assert all(isinstance(row, list) for row in values)
        assert len(values[0]) == len(self.service.COLUMNS)
        assert len(values[1]) == len(self.service.COLUMNS)

    def test_update_row_writes_single_row_not_transposed(self):
        """Verify update_row writes a single row, not transposed columns."""
        # Start with a sheet that has header + 1 data row
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            row_data = self.service._map_metadata_to_row(
                _make_metadata(title="Book 1"), "/c1.jpg"
            )
            sheet.grid = [list(self.service.COLUMNS), row_data]
            # Now update only Publisher and Language columns
            self.service.update_row(2, {"Publisher": "Gallimard", "Language": "French"})
        values = sheet.get_all_values()
        # Verify the row has Publisher and Language in correct columns
        col_map = {name: idx for idx, name in enumerate(self.service.COLUMNS)}
        assert values[1][col_map["Publisher"]] == "Gallimard"
        assert values[1][col_map["Language"]] == "French"
        # Verify NO other rows were created (no transposition)
        assert len(values) == 2  # header + 1 data row only

    def test_update_row_non_contiguous_columns_preserves_alignment(self):
        """Verify update_row with non-adjacent columns does not shift values."""
        sheet = FakeSheet(self.service.COLUMNS)
        with self._patch_get_sheet(sheet):
            row_data = self.service._map_metadata_to_row(
                _make_metadata(title="Book 1"), "/c1.jpg"
            )
            sheet.grid = [list(self.service.COLUMNS), row_data]
            # Update Publisher (col D) and Language (col F), skipping Categories (col G)
            self.service.update_row(2, {"Publisher": "Gallimard", "Language": "French"})
        values = sheet.get_all_values()
        col_map = {name: idx for idx, name in enumerate(self.service.COLUMNS)}
        assert values[1][col_map["Publisher"]] == "Gallimard"
        assert values[1][col_map["Language"]] == "French"
        assert len(values) == 2  # no extra rows created

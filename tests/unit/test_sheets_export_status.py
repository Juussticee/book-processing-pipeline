"""Tests for the customer-facing Status column in the Google Sheet export.

Every test uses mocks — no Google API call and no quota is consumed.

The Status label is the customer's only signal that a book identification is
uncertain, so the rules are asserted exhaustively here:

* ``Needs Review``  — the identification was not accepted (or is ambiguous);
* ``Incomplete``    — identity accepted, but a core field or the cover is missing;
* ``Confirmed``     — identity accepted and every core field is present.
"""

from unittest.mock import Mock, patch

import pytest

from models.book_metadata import BookMetadata
from services.google_sheets_service import GoogleSheetsService


def _meta(**overrides) -> BookMetadata:
    data = {
        "title": "Le coeur vert",
        "authors": ["Jean-Louis Kuffer"],
        "isbn_13": "9782825104699",
        "publisher": "L'Age d'Homme",
        "published_date": "1993",
        "language": "fr",
        "categories": ["Roman"],
        "description": "Un roman.",
        "cover_url": "https://books.google.com/books/content?id=abc",
        "confidence": 0.95,
        "source": "google_books",
        "status": "identified",
        "drive_file_id": "drive001",
        "job_id": "job001",
    }
    data.update(overrides)
    return BookMetadata(**data)


def _service() -> GoogleSheetsService:
    return GoogleSheetsService(
        credentials_file="test_creds.json",
        sheets_id="test_sheet_id_123",
        tab_name="Books",
    )


def _sheet_with(*rows):
    """A mocked worksheet whose ``get_all_values`` returns *rows*."""
    sheet = Mock()
    sheet.get_all_values.return_value = list(rows)
    sheet.update = Mock()
    sheet.append_rows = Mock()
    sheet.insert_cols = Mock()
    return sheet


def _cover_writes(sheet):
    return [
        call.kwargs
        for call in sheet.update.call_args_list
        if call.kwargs.get("value_input_option") == "USER_ENTERED"
    ]


# ---------------------------------------------------------------------------
# 1-8, 15: label rules
# ---------------------------------------------------------------------------


class TestStatusRules:
    def setup_method(self):
        self.service = _service()

    def test_confirmed_when_identity_and_core_fields_are_complete(self):
        assert self.service.export_status(_meta()) == "Confirmed"

    def test_incomplete_when_publisher_is_missing(self):
        # A missing publisher is not an identity failure, but the record is
        # incomplete for the customer.
        assert self.service.export_status(_meta(publisher=None)) == "Incomplete"
        assert self.service.export_status(_meta(publisher="")) == "Incomplete"

    def test_confirmed_when_only_description_is_missing(self):
        # Enrichment fields (description/date/language/categories) never change
        # the label.
        assert self.service.export_status(_meta(description=None)) == "Confirmed"
        assert self.service.export_status(_meta(categories=[])) == "Confirmed"
        assert self.service.export_status(_meta(published_date=None)) == "Confirmed"
        assert self.service.export_status(_meta(language=None)) == "Confirmed"

    def test_incomplete_when_cover_is_missing(self):
        assert (
            self.service.export_status(
                _meta(cover_url=None, thumbnail_url=None), cover_available=False
            )
            == "Incomplete"
        )

    def test_confirmed_when_isbn_is_missing(self):
        """Many books have no ISBN; that alone must not downgrade a record."""
        assert (
            self.service.export_status(_meta(isbn_13=None, isbn_10=None)) == "Confirmed"
        )
        assert self.service.export_status(_meta(isbn_13=None)) == "Confirmed"
        assert (
            self.service.export_status(_meta(isbn_13=None, isbn_10="2825104691"))
            == "Confirmed"
        )

    def test_needs_review_for_review_status(self):
        assert (
            self.service.export_status(_meta(status="needs_review")) == "Needs Review"
        )
        assert (
            self.service.export_status(_meta(status="manual_review")) == "Needs Review"
        )

    def test_review_record_with_complete_metadata_is_still_needs_review(self):
        # Complete fields must never override a review decision.
        metadata = _meta(status="needs_review")
        assert self.service.export_status(metadata) == "Needs Review"

    def test_placeholder_record_is_needs_review(self):
        # What a failed/unreadable processing run persists.
        metadata = _meta(title="(unreadable)", confidence=0.0, status="needs_review")
        assert self.service.export_status(metadata) == "Needs Review"
        assert (
            self.service.export_status(
                _meta(title="(unreadable)", confidence=0.0, status="identified")
            )
            == "Needs Review"
        )

    def test_unknown_or_missing_state_is_never_confirmed(self):
        for status in (None, "", "   ", "identifiedd", "rejected", "duplicate"):
            assert (
                self.service.export_status(_meta(status=status)) == "Needs Review"
            ), status

    def test_missing_title_or_confidence_is_never_confirmed(self):
        # BookMetadata rejects an empty title, so a blank-ish title is the case
        # that can actually be persisted.
        assert self.service.export_status(_meta(title="   ")) == "Needs Review"
        # BookMetadata validates confidence as a number in [0, 1], so the
        # persisted value is always numeric; a zero score is still review-worthy.
        assert self.service.export_status(_meta(confidence=0.0)) == "Needs Review"


# ---------------------------------------------------------------------------
# 11-13: layout migration
# ---------------------------------------------------------------------------


class TestLayoutMigration:
    def setup_method(self):
        self.service = _service()

    def test_legacy_sheet_gets_status_column_inserted_at_a(self):
        legacy_row = [
            "Le coeur vert",
            "Jean-Louis Kuffer",
            "9782825104699",
            "L'Age d'Homme",
            "1993",
            "French",
            "Roman",
            "Un roman.",
            '=IMAGE("https://example.com/cover.jpg")',
        ]
        sheet = _sheet_with(
            [
                "Title",
                "Authors",
                "ISBN-13",
                "Publisher",
                "Published Date",
                "Language",
                "Categories",
                "Description",
                "Open Cover",
            ],
            legacy_row,
        )

        self.service._ensure_layout(sheet)

        sheet.insert_cols.assert_called_once_with([["Status"]], 1)
        # Nothing else is written: no row rewrite, no append, no clear.
        sheet.update.assert_not_called()
        sheet.append_rows.assert_not_called()

    def test_sheet_that_already_has_status_is_not_touched(self):
        sheet = _sheet_with(["Status", "Title", "Authors"], ["Confirmed", "T", "A"])

        self.service._ensure_layout(sheet)

        sheet.insert_cols.assert_not_called()
        sheet.update.assert_not_called()

    def test_empty_sheet_gets_the_full_header(self):
        sheet = _sheet_with()

        self.service._ensure_layout(sheet)

        sheet.insert_cols.assert_not_called()
        kwargs = sheet.update.call_args.kwargs
        assert kwargs["values"] == [GoogleSheetsService.COLUMNS]
        assert kwargs["range_name"] == "A1:J1"
        assert kwargs["value_input_option"] == "RAW"

    def test_layout_check_runs_once_per_service(self):
        sheet = _sheet_with(["Title", "Authors"])

        self.service._ensure_layout(sheet)
        self.service._ensure_layout(sheet)

        assert sheet.get_all_values.call_count == 1
        assert sheet.insert_cols.call_count == 1

    def test_migration_keeps_existing_cover_formula_cell_untouched(self):
        sheet = _sheet_with(
            ["Title", "Open Cover"],
            ["Book", '=IMAGE("https://example.com/cover.jpg")'],
        )

        self.service._ensure_layout(sheet)

        # Only a column insert — no cell is rewritten, so the formula survives.
        assert sheet.update.call_count == 0
        assert sheet.append_rows.call_count == 0


# ---------------------------------------------------------------------------
# 9, 10, 14: row writes
# ---------------------------------------------------------------------------


class TestStatusOnRowWrites:
    def setup_method(self):
        self.service = _service()

    def test_new_row_carries_the_status_label(self):
        sheet = _sheet_with()
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(self.service, "_ensure_external_url_access"):
                self.service.get_or_create_row(_meta(), None)

        appended = sheet.append_rows.call_args.args[0][-1]
        assert appended[0] == "Confirmed"
        assert appended[1] == "Le coeur vert"

    def test_repeated_export_updates_the_same_row_without_appending(self):
        canonical = self.service._map_metadata_to_row(_meta(), None)
        sheet = _sheet_with(list(GoogleSheetsService.COLUMNS), canonical)
        spreadsheet = Mock()
        spreadsheet.fetch_sheet_metadata.return_value = {
            "sheets": [
                {
                    "data": [
                        {
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {
                                                "formulaValue": canonical[9]
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(
                self.service, "_get_spreadsheet", return_value=spreadsheet
            ):
                first = self.service.get_or_create_row(_meta(), None)
                second = self.service.get_or_create_row(_meta(), None)

        assert first == second == 2
        sheet.append_rows.assert_not_called()

    def test_review_resolved_to_confirmed_updates_the_same_row(self):
        """A row labelled Needs Review must become Confirmed in place."""
        review_meta = _meta(status="needs_review")
        canonical = self.service._map_metadata_to_row(review_meta, None)
        assert canonical[0] == "Needs Review"

        sheet = _sheet_with(list(GoogleSheetsService.COLUMNS), list(canonical))
        resolved = _meta()  # same title + author, identity now accepted

        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(self.service, "_ensure_external_url_access"):
                row_id = self.service.get_or_create_row(resolved, None, row_id=2)

        assert row_id == 2
        sheet.append_rows.assert_not_called()
        written = [
            call.kwargs["values"][0]
            for call in sheet.update.call_args_list
            if call.kwargs.get("value_input_option") == "RAW"
        ]
        assert written, "expected the metadata row to be rewritten"
        assert written[0][0] == "Confirmed"

    def test_leading_zero_isbn_is_written_raw_and_keeps_confirmed(self):
        metadata = _meta(isbn_13="0012345678")
        sheet = _sheet_with(
            list(GoogleSheetsService.COLUMNS),
            ["", "Le coeur vert", "Jean-Louis Kuffer", "", "", "", "", "", "", ""],
        )

        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(self.service, "_ensure_external_url_access"):
                self.service.get_or_create_row(metadata, None)

        raw_rows = [
            call.kwargs
            for call in sheet.update.call_args_list
            if call.kwargs.get("value_input_option") == "RAW"
        ]
        assert raw_rows
        values = raw_rows[0]["values"][0]
        assert values[0] == "Confirmed"
        assert values[3] == "0012345678"

    def test_successful_export_never_makes_a_review_record_confirmed(self):
        """The label must come from the record, not from the export succeeding."""
        sheet = _sheet_with()
        review = _meta(
            status="needs_review", isbn_13="9782825104699", publisher="L'Age d'Homme"
        )
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(self.service, "_ensure_external_url_access"):
                row_id = self.service.get_or_create_row(review, None)

        assert row_id == 2  # the export did succeed
        appended = sheet.append_rows.call_args.args[0][-1]
        assert appended[0] == "Needs Review"

    def test_confirmed_record_needs_no_isbn_to_stay_confirmed(self):
        sheet = _sheet_with()
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(self.service, "_ensure_external_url_access"):
                self.service.get_or_create_row(_meta(isbn_13=None, isbn_10=None), None)

        appended = sheet.append_rows.call_args.args[0][-1]
        assert appended[0] == "Confirmed"
        assert appended[3] == ""

    def test_cover_formula_still_written_with_user_entered(self):
        sheet = _sheet_with()
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            with patch.object(self.service, "_ensure_external_url_access"):
                self.service.get_or_create_row(_meta(), None)

        formulas = _cover_writes(sheet)
        assert formulas
        assert formulas[0]["values"] == [
            ['=IMAGE("https://books.google.com/books/content?id=abc")']
        ]
        assert formulas[0]["range_name"] == "J2"


class TestRefreshStatuses:
    """`refresh_statuses()` is the one-off relabel of legacy (blank) rows."""

    def setup_method(self):
        self.service = _service()

    def _legacy_sheet(self):
        return _sheet_with(
            list(GoogleSheetsService.COLUMNS),
            [
                "",
                "Le coeur vert",
                "Jean-Louis Kuffer",
                "9782825104699",
                "L'Age d'Homme",
                "1993",
                "French",
                "Roman",
                "Un roman.",
                '=IMAGE("https://example.com/cover.jpg")',
            ],
        )

    def test_blank_status_is_filled_from_the_row_the_customer_sees(self):
        sheet = self._legacy_sheet()
        # Deliberately poorer metadata: the row itself carries ISBN, publisher
        # and cover, so the label must be Confirmed, not Incomplete.
        metadata = _meta(publisher=None, isbn_13=None, cover_url=None)

        with patch.object(self.service, "_get_sheet", return_value=sheet):
            counts = self.service.refresh_statuses([metadata])

        assert counts == {"updated": 1, "unchanged": 0, "missing": 0}
        kwargs = sheet.update.call_args.kwargs
        assert kwargs["values"] == [["Confirmed"]]
        assert kwargs["range_name"] == "A2"
        assert kwargs["value_input_option"] == "RAW"

    def test_review_row_is_downgraded_in_place(self):
        sheet = _sheet_with(
            list(GoogleSheetsService.COLUMNS),
            [
                "Confirmed",
                "Le coeur vert",
                "Jean-Louis Kuffer",
                "9782825104699",
                "L'Age d'Homme",
                "1993",
                "French",
                "Roman",
                "Un roman.",
                '=IMAGE("https://example.com/cover.jpg")',
            ],
        )
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            counts = self.service.refresh_statuses([_meta(status="manual_review")])

        assert counts["updated"] == 1
        assert sheet.update.call_args.kwargs["values"] == [["Needs Review"]]

    def test_correct_label_is_not_rewritten(self):
        sheet = self._legacy_sheet()
        sheet.get_all_values.return_value[1][0] = "Confirmed"

        with patch.object(self.service, "_get_sheet", return_value=sheet):
            counts = self.service.refresh_statuses([_meta()])

        assert counts == {"updated": 0, "unchanged": 1, "missing": 0}
        sheet.update.assert_not_called()

    def test_record_absent_from_the_sheet_is_reported_not_appended(self):
        sheet = self._legacy_sheet()
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            counts = self.service.refresh_statuses([_meta(title="Not In Sheet")])

        assert counts == {"updated": 0, "unchanged": 0, "missing": 1}
        sheet.append_rows.assert_not_called()
        sheet.update.assert_not_called()

    def test_sheet_without_status_column_is_left_alone(self):
        sheet = _sheet_with(["Title", "Authors"], ["Book", "Author"])
        with patch.object(self.service, "_get_sheet", return_value=sheet):
            counts = self.service.refresh_statuses([_meta()])

        assert counts == {"updated": 0, "unchanged": 0, "missing": 0}
        sheet.update.assert_not_called()


class TestStatusColumnInHeader:
    def test_status_is_the_first_canonical_column(self):
        assert GoogleSheetsService.COLUMNS[0] == "Status"
        assert GoogleSheetsService.COLUMNS[-1] == "Open Cover"
        assert len(GoogleSheetsService.COLUMNS) == 10

    def test_legacy_header_resolves_columns_by_name(self):
        service = _service()
        header = service._read_header(
            [
                [
                    "Title",
                    "Authors",
                    "ISBN-13",
                    "Publisher",
                    "Published Date",
                    "Language",
                    "Categories",
                    "Description",
                    "Open Cover",
                ]
            ]
        )
        # Status is appended, never injected between existing columns.
        assert header[0] == "Title"
        assert header[-1] == "Status"
        row = service._row_values_for_header(_meta(), None, header)
        assert row[0] == "Le coeur vert"
        assert row[8].startswith('=IMAGE("')
        assert row[9] == "Confirmed"


@pytest.mark.parametrize("confidence", [0.0, 0.25, 0.49])
def test_low_confidence_identified_records_are_not_confirmed(confidence):
    service = _service()
    label = service.export_status(_meta(confidence=confidence))
    assert label != "Confirmed"

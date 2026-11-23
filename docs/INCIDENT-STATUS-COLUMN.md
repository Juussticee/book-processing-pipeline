# Incident: a read-only diagnostic mutated a production workbook

**Status:** code defect fixed and covered by regression tests.
**Production data:** one column header added by the incident; no row, cell value
or formula was lost. No further write has been made.
**Approval required:** yes — completing or reversing the change on the live
workbook is an operator decision that has not been taken.

---

## 1. What happened

An operator ran a diagnostic script to print the current state of the
spreadsheet (`scripts/read_sheet_state.py`). The script called
`GoogleSheetsService.read_all_rows()`, whose job is to read. That call went
through `GoogleSheetsService._get_sheet()`, which — before this fix — also ran the
`Status`-column **migration** as a side effect of merely opening the worksheet.

Result: the migration executed during a read. Google Sheets inserted one column
at position A and labelled it `Status`. Every existing column shifted one
position to the right.

## 2. When it was detected

Immediately after the run, while reviewing the script's own output: the printed
header began with `Status`, which the command was not supposed to be able to
change.

## 3. Root cause

A single method did two jobs, and every caller got both:

```python
def _get_sheet(self):                  # used to mutate the sheet
    ...
    self._ensure_layout(self._sheet)   # insert Status at A if missing
    return self._sheet
```

`_ensure_layout()` is a legitimate, one-time write for the **export path**: a
legacy workbook needs the `Status` column before rows can be written. But because
it was attached to the *accessor* rather than to the write operations, any read
migrated the workbook — including a diagnostic run that only wanted to look.

The failure was invisible in tests because the read paths under test used a mocked
worksheet: a mock happily "inserts" a column and nothing notices.

## 4. Current state of the production workbook

Re-verified read-only on 2026-11-23 (`_get_sheet(ensure_layout=False)` and
`get_all_values(value_render_option="FORMULA")`; no write call was made):

| Measurement | Value |
|---|---|
| Rows | 19 — one header plus **18 data rows** |
| Columns | 10 — `Status`, `Title`, `Authors`, `ISBN-13`, `Publisher`, `Published Date`, `Language`, `Categories`, `Description`, `Open Cover` |
| `Status` cells | 18, **all empty**, no formulas |
| Row content | every original record present; 18 titles and authors, 13 publishers, 16 dates, 18 languages, 10 categories, 3 descriptions, 6 ISBN-13 values |
| Cover column | 14 live `=IMAGE(…)` formulas, intact |
| Metadata formulas | none — the columns hold plain values, so the shift reinterpreted nothing |

The header order now matches the application's canonical column order, so the
workbook is structurally consistent with the code. Only the labels are missing.

## 5. What was and was not changed

**Changed by the incident:** one inserted column (A) and its header text
(`Status`). Existing cells moved one column right, which is what a column insert
does.

**Not changed:** no row was deleted, no row was appended, no cell value was
rewritten, no cover formula was altered, no label was written, no formula was
recalculated into a different value.

**Not done afterwards:** no rollback, no refresh, no export, no further write of
any kind. The workbook is exactly as the incident left it.

## 6. The fix

`services/google_sheets_service.py`:

- `_get_sheet(ensure_layout: bool = True)` and
  `read_all_rows(ensure_layout: bool = True)` gained an explicit switch. Write
  paths keep the default; diagnostic reads pass `False`.
- A read-only open can no longer **create** a worksheet or a header row either.
  Previously, if the tab was missing or misnamed, the accessor's `except` branch
  called `add_worksheet()` and wrote a header — the same class of bug through a
  different door. That branch now raises instead:

  ```
  RuntimeError: Worksheet '<name>' is not available in the configured
  spreadsheet; refusing to create it on a read-only path (<ExcType>)
  ```

- `scripts/read_sheet_state.py` was rewritten as a strictly read-only snapshot:
  it opens with `ensure_layout=False`, reads with
  `value_render_option="FORMULA"`, and prints the header, the column count and a
  truncated preview. It has no write path.
- `_e2e_verify.py` (development verification) reads with
  `read_all_rows(ensure_layout=False)` for the same reason.

Migration is now reachable only from a write path — `export_status`/row export,
`refresh_statuses`, or the isolated acceptance script — and it logs when it runs.

## 7. Regression tests

`tests/unit/test_google_sheets_service.py::TestReadOnlySheetAccess` pins the
behaviour with mocked worksheets:

| Test | Asserts |
|---|---|
| `test_read_all_rows_can_skip_migration` | `read_all_rows(ensure_layout=False)` never calls `insert_cols` |
| `test_default_read_keeps_migration_for_write_paths` | the default still migrates, because write paths depend on it |
| `test_cached_sheet_is_migrated_only_when_asked` | a cached worksheet migrates only on a `ensure_layout=True` call |
| `test_read_only_open_never_creates_a_worksheet` | a missing tab raises instead of creating a tab |
| `test_read_all_rows_read_only_never_creates_a_worksheet` | same, through `read_all_rows` |
| `test_write_path_still_creates_the_worksheet` | the write path still creates the tab and header |

## 8. Lessons applied

- A method that both returns a resource and mutates it is a trap. The default is
  now the safe-for-writers behaviour and the read-only behaviour is explicit at
  every call site, including scripts.
- Every diagnostic script was audited for write capability, not just this one.
- The production workbook is never used to test a diagnostic tool. Isolated
  spreadsheets are documented in `docs/RUNBOOK.md` §13.

## 9. Outstanding decision (not taken here)

The live workbook has a `Status` column whose labels are empty. Two options,
both requiring explicit operator approval:

1. **Complete it** — take a full backup, then
   `python app.py --refresh-sheet-status`, which writes **only** the Status cell
   of rows it can match by title/author and never appends or deletes. Verify that
   only column A changed.
2. **Reverse it** — delete column A and keep the previous nine-column layout.
   The application would re-insert the column on the next export, so this only
   makes sense if the workbook is intentionally frozen.

`docs/RUNBOOK.md` §12.1 and §13.1 document the backup, verification and rollback
steps for both.

# Environment Variables

This document explains every environment variable used by Book Processor.

## Application

### APP_NAME

Description:
The name of the application. Used in the startup banner and logger name.

Required:
No

Default:
`Book Processor`

Example:
`Book Processor`

Milestone:
Milestone 1

---

### VERSION

Description:
The current version of the application. Displayed in the startup banner.

Required:
No

Default:
`0.1.0`

Example:
`0.1.0`

Milestone:
Milestone 1

---

### ENVIRONMENT

Description:
The runtime environment. Controls log verbosity and behaviour differences between development and production.

Required:
No

Default:
`Development`

Example:
`Development`

Milestone:
Milestone 1

---

### LOG_LEVEL

Description:
The minimum log level to record. Valid values are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`.

Required:
No

Default:
`INFO`

Example:
`DEBUG`

Milestone:
Milestone 1

---

## Paths

### DATA_DIRECTORY

Description:
The root directory where application data is stored.

Required:
No

Default:
`data`

Example:
`data`

Milestone:
Milestone 1

---

### LOG_DIRECTORY

Description:
The directory where log files are written.

Required:
No

Default:
`logs`

Example:
`logs`

Milestone:
Milestone 1

---

### TEMP_DIRECTORY

Description:
The directory used for temporary files during processing.

Required:
No

Default:
`data/temp`

Example:
`data/temp`

Milestone:
Milestone 1

---

## Google Drive

### GOOGLE_DRIVE_FOLDER_ID

Description:
The ID of the Google Drive folder that the application monitors for book files.

Required:
Yes (Milestone 2)

Default:
(empty)

Example:
`1X7A3N6kfj29Lxxxxx`

How to obtain:

1. Open Google Drive.
2. Navigate to the folder you want the application to monitor.
3. Copy the URL from your browser address bar.
4. Example URL:
   ```
   https://drive.google.com/drive/folders/1X7A3N6kfj29Lxxxxx
   ```
5. The folder ID is everything after `folders/`:
   ```
   1X7A3N6kfj29Lxxxxx
   ```

Milestone:
Milestone 2

---

### GOOGLE_SERVICE_ACCOUNT_FILE

Description:
The path to the Google Cloud service account JSON key file. The application uses this file to authenticate with Google APIs.

Required:
Yes (Milestone 2)

Default:
`credentials/service_account.json`

Example:
`credentials/service_account.json`

How to obtain:

1. Create a service account in Google Cloud Console (see `docs/google_setup.md`).
2. Download the JSON key file.
3. Place it in the `credentials/` directory of the project.

Milestone:
Milestone 2

---

## Google Sheets

### GOOGLE_SHEET_ID

Description:
The ID of the Google Sheet used to store processing results and metadata.

Required:
Yes (Milestone 4)

Default:
(empty)

Example:
`1AbCdEfGhIjKlMnOpQrStUvWxYz`

How to obtain:

1. Open Google Sheets.
2. Open the desired spreadsheet.
3. Copy the URL from your browser address bar.
4. Example URL:
   ```
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit
   ```
5. The Sheet ID is everything between `/d/` and `/edit`:
   ```
   1AbCdEfGhIjKlMnOpQrStUvWxYz
   ```

Milestone:
Milestone 4

---

## OCR

### OCR_LANGUAGE

Description:
The language code used by the OCR engine when extracting text from images. Uses ISO 639-1 language codes.

Required:
No

Default:
`en`

Example:
`en`

Available values:
`en` (English), `es` (Spanish), `fr` (French), `de` (German), `ja` (Japanese), and others supported by Tesseract.

Milestone:
Milestone 5 (OCR)

---

## OCR
### OCR_LANGUAGE
... (existing content) ...
Milestone: Milestone 5 (OCR)

---

### OCR_CONFIDENCE_THRESHOLD

Description:
The minimum confidence threshold for OCR-extracted words. Words below this threshold are filtered out before being used for downstream tasks. Set to `0.5` by default. Lowering this value includes more (possibly noisy) words in OCR output.

Required:
No

Default:
`0.5`

Example:
`0.6`

Milestone:
Milestone 4

---

### OCR_USE_GPU

Description:
Whether to use GPU acceleration for the OCR engine. When enabled, EasyOCR will attempt to use CUDA for text extraction. Set to `false` on machines without GPU support.

Required:
No

Default:
`false`

Example:
`true`

Milestone:
Milestone 4

---

## Book Lookup

### GOOGLE_BOOKS_TIMEOUT

Description:
Timeout in seconds for the Google Books API. The API typically responds within a few seconds, but the timeout may need to be increased on slow networks.

Required:
No

Default:
`15`

Example:
`30`

Milestone:
Milestone 4

---

### OPEN_LIBRARY_ENABLED

Description:
Whether to use the Open Library API as a fallback when Google Books does not return results. When set to `false`, only Google Books is used.

Required:
No

Default:
`true`

Example:
`false`

Milestone:
Milestone 4

---

### OPEN_LIBRARY_TIMEOUT

Description:
Timeout in seconds for the Open Library API. This is typically lower than the Google Books timeout since Open Library is a secondary source.

Required:
No

Default:
`10`

Example:
`30`

Milestone:
Milestone 4

---

### CONFIDENCE_THRESHOLD

Description:
The minimum confidence score required for a book identification result to be accepted (as opposed to being flagged for manual review). Results with confidence below this threshold are marked `MANUAL_REVIEW` and require human verification. The confidence score is calculated using ISBN match weight (0.50), title (0.25), author (0.15), publisher (0.05), OCR quality (0.05), and source bonus.

Required:
No

Default:
`0.80`

Example:
`0.70`

Milestone: Milestone 4

---

### CONFIDENCE_MANUAL_REVIEW_THRESHOLD

Description:
The confidence score below the auto-accept threshold at which a book identification result is escalated for manual review instead of being automatically accepted. Results with confidence between `CONFIDENCE_REJECTED_THRESHOLD` and `CONFIDENCE_THRESHOLD` are flagged for human verification.

Required: No

Default: `0.50`

Example: `0.60`

Milestone: Milestone 8

---

### CONFIDENCE_REJECTED_THRESHOLD

Description:
The confidence score below which a book identification result is considered a non-match and rejected. Results with confidence below this threshold are discarded. The rejected threshold must not exceed the manual review threshold.

Required: No

Default: `0.50`

Example: `0.30`

Milestone: Milestone 8

---

### GOOGLE_BOOKS_API_KEY

Description:
The API key for the Google Books API. Used to fetch book metadata (title, author, cover image) from Google Books.

Required:
No

Default:
(empty)

Example:
`AIzaSyD...your-key-here...`

How to obtain:

1. Go to the Google Cloud Console.
2. Navigate to APIs & Services > Library.
3. Search for Google Books API and enable it.
4. Navigate to APIs & Services > Credentials.
5. Click Create Credentials > API Key.
6. Copy the key and add it to your .env file.

Free-tier limitations:
The Google Books API has a free tier of 1,000 requests per day. No billing account is required for the free tier.

Milestone:
Milestone 6 (Future)

---

## Export Pipeline (Milestone 5)

### DEBUG_RETENTION_DAYS

Description:
The number of days to retain debug artifact directories under `data/debug/<job_id>/`. The sync engine runs cleanup at the end of each sync cycle, removing debug artifact directories older than this period. Set to `0` to disable cleanup (retain all debug artifacts indefinitely).

Required:
No

Default:
`3`

Example:
`7`

Milestone:
Milestone 8

---

### GOOGLE_SHEETS_CREDENTIALS

Description:
The path to the Google service account JSON key file used for authenticating with the Google Sheets API during the export pipeline.

Required:
No (only needed when running the export pipeline)

Default:
`credentials/service_account.json`

Example:
`credentials/service_account.json`

Milestone:
Milestone 5

---

### GOOGLE_SHEETS_TAB_NAME

Description:
The name of the worksheet tab within the Google Sheets document where book metadata is exported.

Required:
No

Default:
`Sheet1`

Example:
`Books`

Milestone:
Milestone 5

---

### COVERS_DIR

Description:
The directory where downloaded cover images are stored.

Required:
No

Default:
`data/covers`

Example:
`data/covers`

Milestone:
Milestone 5

---

### COVER_DOWNLOAD_TIMEOUT

Description:
The timeout in seconds for downloading cover images from URLs.

Required:
No

Default:
`10`

Example:
`30`

Milestone:
Milestone 5

---

### COVER_MIN_WIDTH

Description:
The minimum acceptable width in pixels for a cover image. Images with dimensions below this threshold are rejected.

Required:
No

Default:
`200`

Example:
`300`

Milestone:
Milestone 5

---

### COVER_MIN_HEIGHT

Description:
The minimum acceptable height in pixels for a cover image. Images with dimensions below this threshold are rejected.

Required:
No

Default:
`200`

Example:
`300`

Milestone:
Milestone 5

---

### EXPORT_MAX_RETRIES

Description:
The maximum number of retry attempts for failed export operations (metadata validation, cover download, Sheets export).

Required:
No

Default:
`3`

Example:
`5`

Milestone:
Milestone 5

---

### EXPORT_RETRY_DELAY

Description:
The delay in seconds between export retry cycles.

Required:
No

Default:
`60`

Example:
`120`

Milestone:
Milestone 5

---

## Quick Reference

|| Variable | Required | Default | Milestone |
||---|---|---|---|
|| `APP_NAME` | No | `Book Processor` | 1 |
|| `VERSION` | No | `0.1.0` | 1 |
|| `ENVIRONMENT` | No | `Development` | 1 |
|| `LOG_LEVEL` | No | `INFO` | 1 |
|| `DATA_DIRECTORY` | No | `data` | 1 |
|| `LOG_DIRECTORY` | No | `logs` | 1 |
|| `TEMP_DIRECTORY` | No | `data/temp` | 1 |
|| `COVERS_DIR` | No | `data/covers` | 5 |
|| `GOOGLE_DRIVE_FOLDER_ID` | Yes | (empty) | 3 |
|| `GOOGLE_SERVICE_ACCOUNT_FILE` | Yes | `credentials/service_account.json` | 3 |
|| `GOOGLE_SHEET_ID` | Yes | (empty) | 4 |
|| `OCR_LANGUAGE` | No | `en` | 5 |
|| `GOOGLE_BOOKS_API_KEY` | No | `(empty)` | 6 |
|| `OCR_CONFIDENCE_THRESHOLD` | No | `0.5` | 4 |
|| `OCR_USE_GPU` | No | `false` | 4 |
|| `GOOGLE_BOOKS_TIMEOUT` | No | `15` | 4 |
|| `OPEN_LIBRARY_ENABLED` | No | `true` | 4 |
|| `OPEN_LIBRARY_TIMEOUT` | No | `10` | 4 |
|| `CONFIDENCE_THRESHOLD` | No | `0.80` | 4 |
|| `GOOGLE_SHEETS_CREDENTIALS` | No | `credentials/service_account.json` | 5 |
|| `GOOGLE_SHEETS_TAB_NAME` | No | `Sheet1` | 5 |
|| `COVER_DOWNLOAD_TIMEOUT` | No | `10` | 5 |
|| `COVER_MIN_WIDTH` | No | `200` | 5 |
|| `COVER_MIN_HEIGHT` | No | `200` | 5 |
|| `EXPORT_MAX_RETRIES` | No | `3` | 5 |
|| `EXPORT_RETRY_DELAY` | No | `60` | 5 |
|| `CONFIDENCE_THRESHOLD` | No | `0.80` | 4 |
|| `CONFIDENCE_MANUAL_REVIEW_THRESHOLD` | No | `0.50` | 8 |
|| `CONFIDENCE_REJECTED_THRESHOLD` | No | `0.50` | 8 |
|| `DEBUG_RETENTION_DAYS` | No | `3` | 8 |

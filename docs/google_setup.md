# Google Cloud Setup Guide

This guide explains how to set up every Google credential needed for Book Processor. Follow each step in order. No prior Google Cloud experience is assumed.

---

## Step 1: Create a Google Cloud Project

1. Open a web browser and go to https://console.cloud.google.com/.
2. If you are not signed in, sign in with your Google account.
3. Click the Select a project dropdown at the top of the page (it may say "No project selected").
4. Click New Project at the top-left.
5. In the Project name field, type a name for your project, for example:
   book-processor
6. Click Create.
7. Wait a few seconds for the project to be created.
8. Make sure your new project is selected in the dropdown at the top. You should see its name there.

---

## Step 2: Enable the Google Drive API

1. In the Google Cloud Console, make sure your project is selected (top of the page).
2. In the left sidebar, click APIs & Services.
3. Click Library in the submenu.
4. In the search bar at the top, type Google Drive API.
5. Click Google Drive API in the search results.
6. Click the Enable button.
7. Wait for the page to confirm the API is enabled.

---

## Step 3: Enable the Google Sheets API

1. In the left sidebar, click APIs & Services > Library (if you are not already there).
2. In the search bar, type Google Sheets API.
3. Click Google Sheets API in the search results.
4. Click the Enable button.
5. Wait for the page to confirm the API is enabled.

---

## Step 4: Create a Service Account

A service account is a special Google account that your application uses to authenticate with Google APIs.

1. In the left sidebar, click APIs & Services > Credentials.
2. Click + CREATE CREDENTIALS at the top.
3. Select Service account from the dropdown.
4. In the Service account name field, type a descriptive name, for example:
   book-processor-sa
5. In the Service account ID field, the value will auto-fill based on the name. Leave it as-is.
6. (Optional) In the Service account description field, type:
   Service account for Book Processor application
7. Click Create and then Done.

---

## Step 5: Assign Permissions to the Service Account

The service account needs permission to read and write to Google Drive and Google Sheets.

1. In the Credentials page, find the service account you just created.
2. Click on its name (e.g., book-processor-sa).
3. Click the Grant access or Add roles button (depending on the UI).
4. In the Select a role dropdown, type Editor and select the Editor role.
5. Click Save.

---

## Step 6: Create a JSON Key

1. On the service account details page, click the Keys tab.
2. Click Add Key at the top.
3. Select Create new key.
4. In the dialog, select JSON as the key type.
5. Click Create.
6. A JSON file will be downloaded to your computer automatically. Save it somewhere you can find it.

---

## Step 7: Place the JSON Key in the Project

1. Locate the downloaded JSON file on your computer.
2. Open your project folder: book_processor/
3. Navigate to the credentials/ folder.
4. Copy the JSON file into this folder.
5. Rename it to: service_account.json
6. The final path should be: book_processor/credentials/service_account.json
7. Make sure the file is listed in .gitignore so you do not accidentally commit it to version control.

---

## Step 8: Share the Google Drive Folder with the Service Account

The Google Drive folder you want the application to monitor must be shared with the service account. Without this step, the application will not be able to see the folder contents.

1. Open Google Drive.
2. Create a new folder or select an existing folder you want the application to monitor.
3. Right-click the folder and select Share.
4. In the Add people and groups field, paste the service account email address. You can find this email on the service account details page in Google Cloud Console. It looks like:
   book-processor-sa@your-project-id.iam.gserviceaccount.com
5. Set the permission level to Viewer (at minimum).
6. Click Send.

---

## Step 9: Create the Google Sheet

1. Open Google Sheets.
2. Click Blank to create a new spreadsheet.
3. Give the spreadsheet a name, for example: Book Processor Results
4. Copy the URL from your browser address bar.
5. Example URL:
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit
6. The Sheet ID is the part between /d/ and /edit:
   1AbCdEfGhIjKlMnOpQrStUvWxYz
7. Add this Sheet ID to your .env file:
   GOOGLE_SHEET_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz
   

---

## Step 10: Share the Google Sheet with the Service Account

1. Open the Google Sheet you just created.
2. Click the Share button in the top-right corner.
3. In the Add people and groups field, paste the service account email address (same as Step 8).
4. Set the permission level to Editor.
5. Click Send.

---

## Step 11: Set Up the Google Books API (Optional)

The Google Books API is used to fetch book metadata (title, author, cover image) automatically.

### Do you need an API key?

Yes. The Google Books API requires an API key for authentication.

### How to create one:

1. Go to the Google Cloud Console.
2. Make sure your project is selected.
3. In the left sidebar, click APIs & Services > Library.
4. Search for Google Books API and click it.
5. Click Enable.
6. In the left sidebar, click APIs & Services > Credentials.
7. Click + CREATE CREDENTIALS > API key.
8. Copy the generated key. It will look something like:
   AIzaSyD...your-key-here...
9. Add it to your .env file:
   GOOGLE_BOOKS_API_KEY=AIzaSyD...your-key-here...

### Free-tier limitations:

The Google Books API has a free tier of 1,000 requests per day. No billing account is required for the free tier. If you exceed this limit, requests will be throttled until the next day.

### Where it should be stored:

Store the API key in your .env file as GOOGLE_BOOKS_API_KEY. Never commit the .env file to version control.

---

## Summary Checklist

- [ ] Google Cloud project created
- [ ] Google Drive API enabled
- [ ] Google Sheets API enabled
- [ ] Service account created
- [ ] Service account assigned Editor role
- [ ] JSON key downloaded and placed at credentials/service_account.json
- [ ] Google Drive folder shared with service account email (Viewer access)
- [ ] Google Sheet created
- [ ] Google Sheet ID added to .env
- [ ] Google Sheet shared with service account email (Editor access)
- [ ] (Optional) Google Books API enabled
- [ ] (Optional) API key added to .env as GOOGLE_BOOKS_API_KEY

"""Targeted metadata corrections for known wrong/missing fields."""

import sqlite3

conn = sqlite3.connect('data/metadata.db')
c = conn.cursor()

fixes = [
    # title, publisher, language, published_date, isbn_10, isbn_13, source
    ("Analyses de Vie et Mort de la Yougoslavie", None, "French", None, None, None, "title_inference"),
    ("DEBOUT, DANS L'ORDRE ET L'HARMONIE DE LA CREATION", None, "French", "1983", None, None, "google_books"),
    ("L'individu et la communauté", "L'Âge d'Homme", "French", "1991", None, None, "google_books"),
    ("LA CONSTRUCTION DES VILLES", "L'Âge d'Homme", "French", "1992", None, None, "google_books"),
    ("Weiser David", "L'Âge d'Homme", "French", "1990", None, None, "google_books"),
]

for title, pub, lang, date, isbn10, isbn13, src in fixes:
    sets = []
    vals = []
    if pub:
        sets.append("publisher=?")
        vals.append(pub)
    if lang:
        sets.append("language=?")
        vals.append(lang)
    if date:
        sets.append("published_date=?")
        vals.append(date)
    if isbn10:
        sets.append("isbn_10=?")
        vals.append(isbn10)
    if isbn13:
        sets.append("isbn_13=?")
        vals.append(isbn13)
    if src:
        sets.append("source=?")
        vals.append(src)
    if not sets:
        continue
    vals.append(title)
    sql = f"UPDATE book_metadata SET {', '.join(sets)} WHERE title=?"
    c.execute(sql, vals)
    print(f"Updated {title}: {sets}")

conn.commit()
conn.close()
print('Done.')

"""
Regression test for EPG title/description silently staying stale after a
provider corrects its guide data.

Bug (MEDIUM): EPGProgram rows are inserted with INSERT OR IGNORE against a
unique index on (account_id, epg_id), where
    epg_id = f"{stream_id}:{start_timestamp}:{stop_timestamp}"

When a provider corrects a program title or description (same channel, same
air time, different metadata), the OR IGNORE clause silently discards the
update. The stale title persists until the row's end_time falls below the
archive cutoff and is cleaned up in the next refresh cycle, which can be
days away for channels with long archive windows.

User impact: a user sees the wrong program title in the guide (e.g. a news
special mislabeled by the provider, a live event with a placeholder title,
or a typo corrected by the provider after initial ingest). Re-downloading or
re-scheduling based on the stale guide entry records the wrong content with
the wrong filename.

Root cause: epg_ingest_manager.py line 244
    insert_stmt = insert(EPGProgram).prefix_with("OR IGNORE")

There is no UPDATE or ON CONFLICT DO UPDATE path. Once a row is in the DB,
its title and description are frozen until the row expires.

Fix required: change the insert to ON CONFLICT DO UPDATE SET title=...,
description=..., category=... (leaving start_time, end_time, channel_id
unchanged since those form the identity key).
"""
import sqlite3
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _make_db():
    """Return an in-memory SQLite connection with the minimal EPGProgram schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE epg_programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            epg_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            title TEXT,
            description TEXT,
            start_time REAL,
            end_time REAL,
            UNIQUE(account_id, epg_id)
        )
    """)
    conn.commit()
    return conn


def _insert_or_ignore(conn, account_id, epg_id, title, description=""):
    conn.execute(
        "INSERT OR IGNORE INTO epg_programs "
        "(account_id, epg_id, channel_id, title, description, start_time, end_time) "
        "VALUES (?, ?, 'ch1', ?, ?, 0.0, 3600.0)",
        (account_id, epg_id, title, description),
    )
    conn.commit()


def _fetch_title(conn, account_id, epg_id):
    row = conn.execute(
        "SELECT title FROM epg_programs WHERE account_id=? AND epg_id=?",
        (account_id, epg_id),
    ).fetchone()
    return row[0] if row else None


class StaleTitleOnProviderUpdateTests(unittest.TestCase):
    """INSERT OR IGNORE silently discards provider-corrected titles."""

    def setUp(self):
        self.conn = _make_db()

    def tearDown(self):
        self.conn.close()

    def test_corrected_title_visible_after_provider_update(self):
        """After a provider corrects a program title, the next EPG refresh must
        store the updated title in the guide.

        Bug: INSERT OR IGNORE keeps the first-inserted title forever. A program
        initially stored as 'Placeholder Title' cannot have its title corrected
        to 'Live: World Cup Final' even after the provider fixes its guide data.
        The user sees 'Placeholder Title' until the row expires (could be days).

        Expected behavior: second ingest with the corrected title overwrites the
        stale value; the guide reflects what the provider currently says.
        """
        epg_id = "101:1717228800:1717232400"
        _insert_or_ignore(self.conn, 1, epg_id, "Placeholder Title")
        _insert_or_ignore(self.conn, 1, epg_id, "Live: World Cup Final")

        title = _fetch_title(self.conn, 1, epg_id)
        self.assertEqual(
            title,
            "Live: World Cup Final",
            f"Expected updated title 'Live: World Cup Final', got '{title}'. "
            "INSERT OR IGNORE silently discards provider-corrected titles. "
            "Once a row is inserted, its title is frozen until the row expires. "
            "Fix: use ON CONFLICT DO UPDATE SET title=excluded.title, "
            "description=excluded.description.",
        )

    def test_corrected_description_visible_after_provider_update(self):
        """After a provider corrects a program description, it must be stored.

        Bug: same INSERT OR IGNORE path silently discards description updates.
        """
        epg_id = "101:1717228800:1717232400"
        _insert_or_ignore(self.conn, 1, epg_id, "Movie Night", "TBA")
        _insert_or_ignore(self.conn, 1, epg_id, "Movie Night", "A classic thriller from 1978.")

        row = self.conn.execute(
            "SELECT description FROM epg_programs WHERE account_id=1 AND epg_id=?",
            (epg_id,),
        ).fetchone()
        description = row[0] if row else None
        self.assertEqual(
            description,
            "A classic thriller from 1978.",
            f"Expected updated description, got '{description}'. "
            "INSERT OR IGNORE silently discards provider description updates.",
        )

    def test_first_insert_still_creates_row(self):
        """Sanity: a new epg_id must still be inserted (regression guard)."""
        epg_id = "101:1717315200:1717318800"
        _insert_or_ignore(self.conn, 1, epg_id, "News at Ten")
        title = _fetch_title(self.conn, 1, epg_id)
        self.assertEqual(title, "News at Ten", "New row was not inserted.")

    def test_different_account_can_have_same_epg_id(self):
        """Two accounts can have rows with the same epg_id (regression guard).
        Unique constraint is (account_id, epg_id), not just epg_id.
        """
        epg_id = "101:1717228800:1717232400"
        _insert_or_ignore(self.conn, 1, epg_id, "Account 1 Title")
        _insert_or_ignore(self.conn, 2, epg_id, "Account 2 Title")
        title_1 = _fetch_title(self.conn, 1, epg_id)
        title_2 = _fetch_title(self.conn, 2, epg_id)
        self.assertEqual(title_1, "Account 1 Title")
        self.assertEqual(title_2, "Account 2 Title")


if __name__ == "__main__":
    unittest.main()

# Automatic Database Backups

Mustarrd does not ship a backup or restore feature for its own database.

## Why this is out of scope

The database is a single SQLite file in the config dir, and a user who wants it
backed up can back that directory up with any tool they already trust — the
same one backing up their recordings. Building a scheduled backup with
retention, plus manual back-up-now and restore actions in Settings, buys the
user very little that `cp` and a cron job do not, and it buys the project a
restore path that has to stay correct forever.

The restore path is the real cost, and it is worse than it first looks.
Provider passwords and Plex tokens are encrypted at rest with AES-GCM, and the
key does **not** live in the database — it is a separate `credential_key` file
in the config dir (or the `CATCHUP_CREDENTIAL_KEY` environment variable):

```python
# services/credential_crypto.py — the key is a sibling of the database, not a row in it
def _key_file(self) -> Path:
    return ensure_config_files() / "credential_key"
```

So a database-only backup restored onto a fresh install comes back with every
account and Plex link present but undecryptable — a silent, confusing failure
right at the moment the user is least able to cope with it. The alternatives
are no better: shipping the key inside the backup archive turns a file people
will happily copy to a NAS or paste into a support thread into a complete
credential compromise, and detecting the mismatch on restore means writing and
maintaining a "these accounts need re-entering" reconciliation path.

Restoring an older backup also has to re-run the startup schema migrations in
`database.py`, or it lands a database missing columns the running code expects.

None of that is unsolvable. It is simply a lot of surface, most of it security
surface, for a problem the filesystem already solves.

## Prior requests

- #433 — "Automatic database backup with retention"

"""Core-owned active-staff registry; picker-only use, not authority."""
import sqlite3
from pathlib import Path

from apc_core.active_staff_provider import ActiveStaffProvider


CURRENT_IDENTITY_STAFF = (
    ("BIAS", "Admin"),
    ("BON", "Editor"),
    ("DERRICK", "Admin"),
    ("WAT", "Editor"),
    ("YA", "Editor"),
    ("YIM", "Editor"),
)


class CoreStaffRegistry:
    def __init__(self, database_path: Path):
        self.connection = sqlite3.connect(Path(database_path))

    def migrate(self) -> None:
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS core_active_staff_registry ("
            "name TEXT PRIMARY KEY NOT NULL, "
            "role TEXT NOT NULL, "
            "active INTEGER NOT NULL CHECK(active IN (0, 1))"
            ")"
        )
        self.connection.commit()

    def seed_current_identity_staff_if_empty(self) -> None:
        if self.connection.execute("SELECT 1 FROM core_active_staff_registry LIMIT 1").fetchone() is not None:
            return
        self.replace_fixture_records(CURRENT_IDENTITY_STAFF)

    def replace_fixture_records(self, fixture: object) -> None:
        provider = ActiveStaffProvider(fixture)
        with self.connection:
            self.connection.execute("DELETE FROM core_active_staff_registry")
            self.connection.executemany(
                "INSERT INTO core_active_staff_registry(name, role, active) VALUES (?, ?, 1)",
                [(record.name, record.role) for record in provider.active_staff()],
            )

    def active_staff_provider(self) -> ActiveStaffProvider:
        records = tuple(
            self.connection.execute(
                "SELECT name, role FROM core_active_staff_registry WHERE active=1 ORDER BY name"
            ).fetchall()
        )
        return ActiveStaffProvider(records)

    def close(self) -> None:
        self.connection.close()

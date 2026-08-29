import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from pathlib import Path


class SnapshotContractError(ValueError):
    """The local SQLite snapshot cannot be accepted for APC Core."""


ITEM_TABLE = "MainDB__ITEM"
REQUIRED_ITEM_COLUMNS = {"Item ID", "Description", "Description TH", "Type", "Family"}
CUSTOMER_TABLE = "MainDB__CUST"
REQUIRED_CUSTOMER_COLUMNS = {"Cust ID", "Name"}
AWB_TABLE = "MainDB__AWB"
AWB_IDENTITY_COLUMNS = (
    ("Inv No", "INVOICE.Inv No", "Invoice No", "InvNo"),
    ("AWB", "AWB No", "AWBNo"),
    ("AWB Date", "AWBDate", "Date"),
)
CHANGE_NAME_TABLE = "TempDB__ChangeName"
# Keep this closed duplicate in the manifest contract rather than importing the
# runtime explorer: certification must inventory only schema and avoid runtime
# dependencies/circular coupling.  It intentionally mirrors OrderExplorer.
ORDER_REQUIRED_COLUMNS = {
    "MainDB__ORDER": ("Order No", "Order Date", "Cust ID"),
    "MainDB__ORDER_ITEM": ("Order No", "Line No", "Item ID", "Qty"),
    "MainDB__CUST": ("Cust ID", "Name", "Inv Type"),
    "MainDB__CUST_CON": ("Cust ID", "Com Code"),
    "MainDB__CUST_CONSIGNEE": ("Cust ID", "Consignee"),
    "MainDB__CUST_NOTE": ("Cust ID", "Order", "Invoice"),
    "MainDB__ITEM": ("Item ID", "Description", "Description TH"),
}
SCOPE = "read_only_item_explorer"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_readonly_uri(path), uri=True)


def _capability_status(ready: bool) -> str:
    return "verified" if ready else "unavailable"


def _unavailable_capabilities() -> dict:
    return {
        "items": {"required": True, "ready": True, "status": "verified"},
        "customers": {"ready": False, "status": "unavailable"},
        "usa_name_direct_source": {"available": False},
        "change_name_table": {"available": False},
        "awb_shipments": {"ready": False, "status": "unavailable"},
        "orders": {"ready": False, "status": "unavailable"},
    }


def _snapshot_capabilities(path: Path) -> dict:
    """Return a best-effort, schema-only inventory without changing acceptance."""
    capabilities = _unavailable_capabilities()
    try:
        with _open_readonly(path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

            def columns(table: str) -> set[str]:
                if table not in tables:
                    return set()
                return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}

            item_columns = columns(ITEM_TABLE)
            customer_ready = REQUIRED_CUSTOMER_COLUMNS.issubset(columns(CUSTOMER_TABLE))
            awb_columns = columns(AWB_TABLE)
            awb_ready = AWB_TABLE in tables and all(any(alias in awb_columns for alias in aliases) for aliases in AWB_IDENTITY_COLUMNS)
            orders_ready = all(set(required).issubset(columns(table)) for table, required in ORDER_REQUIRED_COLUMNS.items())
            capabilities.update(
                {
                    "customers": {"ready": customer_ready, "status": _capability_status(customer_ready)},
                    "usa_name_direct_source": {"available": "USA Name" in item_columns},
                    "change_name_table": {"available": CHANGE_NAME_TABLE in tables},
                    "awb_shipments": {"ready": awb_ready, "status": _capability_status(awb_ready)},
                    "orders": {"ready": orders_ready, "status": _capability_status(orders_ready)},
                }
            )
    except sqlite3.Error:
        pass
    return capabilities


def _validate_snapshot(path: Path, *, customer_ready: bool = False) -> int:
    try:
        with _open_readonly(path) as connection:
            integrity_row = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity_row is None or integrity_row[0] != "ok":
                raise SnapshotContractError("SQLite integrity check failed")
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{ITEM_TABLE}")')}
            if not REQUIRED_ITEM_COLUMNS.issubset(columns):
                raise SnapshotContractError("required Item table columns are missing")
            if customer_ready:
                customer_columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{CUSTOMER_TABLE}")')}
                if not REQUIRED_CUSTOMER_COLUMNS.issubset(customer_columns):
                    raise SnapshotContractError("required Customer table columns are missing")
            item_count = connection.execute(f'SELECT COUNT(*) FROM "{ITEM_TABLE}"').fetchone()[0]
    except sqlite3.Error as error:
        raise SnapshotContractError("SQLite snapshot cannot be read") from error
    if type(item_count) is not int or item_count < 1:
        raise SnapshotContractError("Item table is empty")
    return item_count


def _copy_descriptor_to_temporary(source_descriptor: int, state_directory: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".accepted-artifact-", suffix=".part", dir=state_directory)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as destination, os.fdopen(os.dup(source_descriptor), "rb") as source:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish_manifest(output_path: Path, manifest: dict) -> None:
    payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".part", dir=output_path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def certify_snapshot(source_path: Path, output_path: Path, generated_at: str, *, customer_ready: bool = False) -> dict:
    """Copy, validate, hash, and atomically accept a local SQLite artifact."""
    source_path = Path(source_path)
    output_path = Path(output_path)
    try:
        source_descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise OSError
    except OSError as error:
        raise SnapshotContractError("snapshot source is missing") from error
    try:
        return certify_snapshot_descriptor(source_descriptor, source_path, output_path, generated_at, customer_ready=customer_ready)
    finally:
        os.close(source_descriptor)


def certify_snapshot_descriptor(source_descriptor: int, source_path: Path, output_path: Path, generated_at: str, *, customer_ready: bool = False) -> dict:
    """Accept a regular source held open by the caller for the full transaction."""
    source_path = Path(source_path)
    output_path = Path(output_path)
    if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
        raise SnapshotContractError("snapshot source is missing")
    if output_path.resolve() == source_path.resolve():
        raise SnapshotContractError("manifest cannot replace the source snapshot")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    state_directory = output_path.parent.resolve()
    temporary_artifact = _copy_descriptor_to_temporary(source_descriptor, state_directory)
    try:
        item_count = _validate_snapshot(temporary_artifact, customer_ready=customer_ready)
        capabilities = _snapshot_capabilities(temporary_artifact)
        accepted_hash = _sha256(temporary_artifact)
        accepted_path = state_directory / f"accepted_snapshot-{accepted_hash}.sqlite"
        temporary_artifact.chmod(0o444)
        try:
            os.link(temporary_artifact, accepted_path, follow_symlinks=False)
        except FileExistsError:
            descriptor = os.open(accepted_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode) or _sha256_descriptor(descriptor) != accepted_hash:
                    raise SnapshotContractError("accepted artifact hash collision")
            finally:
                os.close(descriptor)
        temporary_artifact.unlink()
    except (OSError, SnapshotContractError):
        temporary_artifact.unlink(missing_ok=True)
        raise

    manifest = {
        "accepted": True,
        "scope": SCOPE,
        "generated_at": generated_at,
        "source_path": str(source_path.absolute()),
        "source_sha256": accepted_hash,
        "accepted_artifact_path": str(accepted_path),
        "accepted_artifact_sha256": accepted_hash,
        "sqlite_integrity": "ok",
        "item_count": item_count,
        "required_item_columns": sorted(REQUIRED_ITEM_COLUMNS),
        "capabilities": capabilities,
    }
    if customer_ready:
        manifest["customer_ready"] = True
        manifest["required_customer_columns"] = sorted(REQUIRED_CUSTOMER_COLUMNS)
    try:
        _publish_manifest(output_path, manifest)
    except OSError as error:
        raise SnapshotContractError("acceptance manifest cannot be published") from error
    return manifest

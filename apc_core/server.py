import argparse
import hashlib
import json
import os
import sqlite3
import stat
from http.server import ThreadingHTTPServer
from pathlib import Path

from .active_staff_provider import ActiveStaffProvider
from .core_staff_registry import CURRENT_IDENTITY_STAFF
from .invoice_conversion_source import InvoiceConversionSource, ReadOnlyInvoiceSourceError
from .invoice_draft_service import InvoiceDraftService
from .invoice_drafts import InvoiceDraftStore
from .item_explorer import ItemExplorer, make_handler
from .order_explorer import OrderExplorer
from .source_invoice_explorer import SourceInvoiceExplorer, ReadOnlySourceInvoiceError
from .awb_explorer import AWBExplorer, ReadOnlySourceContractError as AWBSourceContractError
from .recovery import RecoveryAuthorizer, RecoveryService
from .customer_explorer import CustomerExplorer
from .customer_price_module import CustomerPriceModule
from .snapshot_contract import REQUIRED_CUSTOMER_COLUMNS


class RuntimeContractError(ValueError):
    """The requested Item Explorer runtime does not match an accepted snapshot."""


_REQUIRED_MANIFEST_FIELDS = {
    "accepted": bool,
    "scope": str,
    "generated_at": str,
    "source_path": str,
    "source_sha256": str,
    "accepted_artifact_path": str,
    "accepted_artifact_sha256": str,
    "sqlite_integrity": str,
    "item_count": int,
    "required_item_columns": list,
}


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_legacy_invoice_snapshot(snapshot_path: Path, expected_sha256: str) -> SourceInvoiceExplorer:
    """Open one separately approved staged legacy-invoice snapshot, read-only."""
    if type(expected_sha256) is not str or len(expected_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_sha256):
        raise RuntimeContractError("invalid legacy invoice snapshot hash")
    try:
        snapshot_path = Path(snapshot_path)
        metadata = os.lstat(snapshot_path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError
        if Path(str(snapshot_path) + "-wal").exists() or Path(str(snapshot_path) + "-shm").exists():
            raise ValueError
        descriptor = os.open(snapshot_path, os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, ValueError) as error:
        raise RuntimeContractError("refusing legacy invoice snapshot") from error
    try:
        reader = SourceInvoiceExplorer.from_open_descriptor(descriptor, snapshot_path)
        if reader.source_sha256 != expected_sha256:
            reader.close()
            raise ValueError
        return reader
    except (ReadOnlySourceInvoiceError, OSError, ValueError, sqlite3.Error) as error:
        raise RuntimeContractError("refusing legacy invoice snapshot") from error
    finally:
        os.close(descriptor)


def _read_accepted_manifest(manifest_path: Path) -> tuple[int, Path, dict]:
    try:
        manifest_path = Path(manifest_path).resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if type(manifest) is not dict:
            raise ValueError
        for key, expected_type in _REQUIRED_MANIFEST_FIELDS.items():
            if type(manifest.get(key)) is not expected_type:
                raise ValueError
        if (
            manifest["accepted"] is not True
            or manifest["scope"] != "read_only_item_explorer"
            or manifest["sqlite_integrity"] != "ok"
            or manifest["item_count"] < 1
            or manifest["source_sha256"] != manifest["accepted_artifact_sha256"]
        ):
            raise ValueError
        declared_artifact_path = Path(manifest["accepted_artifact_path"])
        artifact_root = os.environ.get("APC_CORE_ARTIFACT_ROOT")
        if artifact_root:
            artifact_parent = Path(artifact_root).resolve(strict=True)
            artifact_path = (artifact_parent / declared_artifact_path.name).resolve(strict=True)
        else:
            artifact_parent = manifest_path.parent
            artifact_path = declared_artifact_path.resolve(strict=True)
        if artifact_path.parent != artifact_parent:
            raise ValueError
        descriptor = os.open(artifact_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError
            actual_hash = _sha256_descriptor(descriptor)
            if actual_hash != manifest["accepted_artifact_sha256"]:
                raise ValueError
        except Exception:
            os.close(descriptor)
            raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeContractError("refusing invalid accepted-artifact manifest") from error
    return descriptor, artifact_path, manifest


def load_accepted_runtime(manifest_path: Path, *, data_dir: Path | None = None) -> tuple[ItemExplorer, dict]:
    """Construct runtime only from the validated accepted SQLite descriptor."""
    descriptor, artifact_path, manifest = _read_accepted_manifest(manifest_path)
    try:
        return ItemExplorer.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir), manifest
    except (OSError, ValueError, sqlite3.Error) as error:
        raise RuntimeContractError("refusing invalid accepted-artifact manifest") from None
    finally:
        os.close(descriptor)


def load_accepted_customer_runtime(manifest_path: Path, *, data_dir: Path | None = None) -> tuple[ItemExplorer, CustomerExplorer, dict]:
    """Load one customer-declared artifact and reconcile it once before serving."""
    descriptor, artifact_path, manifest = _read_accepted_manifest(manifest_path)
    try:
        if manifest.get("customer_ready") is not True or manifest.get("required_customer_columns") != sorted(REQUIRED_CUSTOMER_COLUMNS):
            raise RuntimeContractError("refusing non-customer-ready accepted artifact")
        item_explorer = ItemExplorer.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir)
        customer_explorer = CustomerExplorer(artifact_path, data_dir=data_dir)
        if customer_explorer.reconciliation_status()["source_sha256"] != manifest["accepted_artifact_sha256"]:
            customer_explorer.close()
            raise RuntimeContractError("refusing mismatched customer accepted artifact")
        customer_explorer.backfill_from_snapshot()
        return item_explorer, customer_explorer, manifest
    except RuntimeContractError:
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        raise RuntimeContractError("refusing invalid customer accepted-artifact manifest") from None
    finally:
        os.close(descriptor)


def load_accepted_customer_price_runtime(manifest_path: Path, *, data_dir: Path | None = None) -> tuple[ItemExplorer, CustomerExplorer, CustomerPriceModule, dict]:
    """Build customer pricing only from one verified accepted snapshot descriptor."""
    descriptor, artifact_path, manifest = _read_accepted_manifest(manifest_path)
    item_explorer = customer_explorer = price_module = None
    try:
        if manifest.get("customer_ready") is not True or manifest.get("required_customer_columns") != sorted(REQUIRED_CUSTOMER_COLUMNS):
            raise RuntimeContractError("refusing non-customer-ready accepted artifact")
        item_explorer = ItemExplorer.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir)
        customer_explorer = CustomerExplorer(artifact_path, data_dir=data_dir)
        if customer_explorer.reconciliation_status()["source_sha256"] != manifest["accepted_artifact_sha256"]:
            raise RuntimeContractError("refusing mismatched customer accepted artifact")
        if customer_explorer.reconciliation_status()["state"] != "ready":
            customer_explorer.backfill_from_snapshot()
        price_module = CustomerPriceModule.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir)
        if price_module.reconciliation_status()["state"] != "ready":
            price_module.import_from_snapshot()
        return item_explorer, customer_explorer, price_module, manifest
    except RuntimeContractError:
        raise
    except (OSError, ValueError, sqlite3.Error) as error:
        raise RuntimeContractError("refusing invalid customer-price accepted artifact") from None
    finally:
        if price_module is None:
            if customer_explorer is not None:
                customer_explorer.close()
            if item_explorer is not None:
                item_explorer.close()
        os.close(descriptor)


def load_accepted_customer_price_order_runtime(manifest_path: Path, *, data_dir: Path | None = None, with_invoice_drafts: bool = False, legacy_invoice_snapshot: Path | None = None, legacy_invoice_sha256: str | None = None) -> tuple:
    """Build order dependencies and optionally one separately verified legacy-invoice reader."""
    if (legacy_invoice_snapshot is None) != (legacy_invoice_sha256 is None):
        raise RuntimeContractError("legacy invoice snapshot path and hash must be supplied together")
    descriptor, artifact_path, manifest = _read_accepted_manifest(manifest_path)
    item_explorer = customer_explorer = price_module = order_explorer = None
    awb_explorer = invoice_source = invoice_draft_service = source_invoice_explorer = None
    try:
        if legacy_invoice_snapshot is None:
            # Fail-closed: the accepted artifact alone must never mount source_invoice
            # routes. Legacy invoices require an explicit, separately verified snapshot.
            source_invoice_explorer = None
        else:
            assert legacy_invoice_sha256 is not None
            source_invoice_explorer = load_verified_legacy_invoice_snapshot(legacy_invoice_snapshot, legacy_invoice_sha256)
        item_explorer = ItemExplorer.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir)
        if source_invoice_explorer is not None:
            item_explorer.attach_source_invoice_explorer(source_invoice_explorer)
        capabilities = manifest.get("capabilities")
        def verified_capability(name: str) -> bool:
            capability = capabilities.get(name) if type(capabilities) is dict else None
            return type(capability) is dict and capability.get("ready") is True and capability.get("status") == "verified"
        if verified_capability("customers"):
            try:
                customer_explorer = CustomerExplorer(artifact_path, data_dir=data_dir)
                if customer_explorer.reconciliation_status()["source_sha256"] != manifest["accepted_artifact_sha256"]:
                    raise ValueError
                if customer_explorer.reconciliation_status()["state"] != "ready":
                    customer_explorer.backfill_from_snapshot()
            except (OSError, ValueError, sqlite3.Error):
                if customer_explorer is not None:
                    customer_explorer.close()
                customer_explorer = None
        if verified_capability("customer_prices"):
            try:
                price_module = CustomerPriceModule.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir)
                if price_module.reconciliation_status()["state"] != "ready":
                    price_module.import_from_snapshot()
            except (OSError, ValueError, sqlite3.Error):
                if price_module is not None:
                    price_module.close()
                price_module = None
        if verified_capability("orders"):
            try:
                order_explorer = OrderExplorer.from_open_descriptor(descriptor, artifact_path)
            except (OSError, ValueError, sqlite3.Error):
                order_explorer = None
        # Shipments are optional: a snapshot exported before the AWB tables were
        # included must still serve every other module. The route and menu card
        # stay absent. Manifest capability is authoritative; absent or malformed
        # declarations deny AWB.
        if verified_capability("awb_shipments"):
            try:
                awb_explorer = AWBExplorer.from_open_descriptor(descriptor, artifact_path)
            except (AWBSourceContractError, OSError, sqlite3.Error):
                awb_explorer = None
        else:
            awb_explorer = None
        if with_invoice_drafts and data_dir is not None:
            candidate_source = candidate_service = None
            try:
                candidate_source = InvoiceConversionSource.from_open_descriptor(
                    descriptor,
                    artifact_path,
                    current_price_lookup=price_module.invoice_current_price if price_module is not None else None,
                )
                if candidate_source.source_sha256 != manifest["accepted_artifact_sha256"]:
                    raise ValueError
                candidate_service = InvoiceDraftService(InvoiceDraftStore(data_dir))
                invoice_source, invoice_draft_service = candidate_source, candidate_service
            except (ReadOnlyInvoiceSourceError, OSError, ValueError, sqlite3.Error):
                if candidate_source is not None:
                    candidate_source.close()
                if candidate_service is not None:
                    candidate_service.store.close()
                invoice_source = invoice_draft_service = None
        if with_invoice_drafts:
            return item_explorer, customer_explorer, price_module, order_explorer, awb_explorer, invoice_source, invoice_draft_service, manifest
        return item_explorer, customer_explorer, price_module, order_explorer, awb_explorer, None, None, manifest
    except (OSError, ValueError, sqlite3.Error) as error:
        if item_explorer is None and source_invoice_explorer is not None:
            source_invoice_explorer.close()
        raise RuntimeContractError("refusing invalid customer-price-order accepted artifact") from None
    finally:
        os.close(descriptor)


def allowed_mutation_origins(*, container_ingress: bool) -> frozenset[str] | None:
    configured = os.environ.get("APC_CORE_ALLOWED_MUTATION_ORIGINS", "")
    origins = frozenset(origin.strip() for origin in configured.split(",") if origin.strip())
    if container_ingress and not origins:
        raise RuntimeContractError("container ingress requires explicit approved Program mutation origin")
    return origins or None


def invoice_drafts_enabled() -> bool:
    """Draft persistence is opt-in: source browsing must not migrate Core SQLite at startup."""
    return os.environ.get("APC_CORE_ENABLE_INVOICE_DRAFTS") == "1"


def recovery_test_mode(*, data_dir: Path) -> tuple[RecoveryAuthorizer | None, RecoveryService | None]:
    """Enable the recovery panel only for an explicitly PIN-configured isolated test process."""
    pin = os.environ.get("APC_CORE_RECOVERY_TEST_PIN")
    if os.environ.get("APC_CORE_RECOVERY_TEST_MODE") == "1":
        return RecoveryAuthorizer.from_state_file(data_dir / "recovery-auth.json"), RecoveryService(data_dir=data_dir)
    if pin is None:
        return None, None
    return RecoveryAuthorizer.from_test_pin(pin), RecoveryService(data_dir=data_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback-only APC Core Item Explorer pilot")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--legacy-invoice-snapshot", type=Path)
    parser.add_argument("--legacy-invoice-sha256")
    parser.add_argument("--port", default=8769, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--container-ingress", action="store_true", help="allow container-network ingress; never publish a host port")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not (args.container_ingress and args.host == "0.0.0.0"):
        parser.error("host must be loopback-only unless explicit container ingress is used")
    recovery_enabled = os.environ.get("APC_CORE_RECOVERY_TEST_PIN") is not None or os.environ.get("APC_CORE_RECOVERY_TEST_MODE") == "1"
    if args.container_ingress and recovery_enabled:
        parser.error("recovery test mode is loopback-only and forbidden with container ingress")
    if recovery_enabled and args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("recovery test mode is loopback-only")
    data_dir = Path(os.environ["APC_CORE_DATA_DIR"]) if os.environ.get("APC_CORE_DATA_DIR") else None
    if recovery_enabled and data_dir is None:
        parser.error("APC_CORE_DATA_DIR is required for isolated recovery test mode")
    try:
        mutation_origins = allowed_mutation_origins(container_ingress=args.container_ingress)
    except RuntimeContractError as error:
        parser.error(str(error))
    recovery_authorizer, recovery_service = recovery_test_mode(data_dir=data_dir or Path("."))
    item_explorer, customer_explorer, customer_price_module, order_explorer, awb_explorer, invoice_source, invoice_draft_service, manifest = load_accepted_customer_price_order_runtime(
        args.manifest, data_dir=data_dir, with_invoice_drafts=invoice_drafts_enabled(),
        legacy_invoice_snapshot=args.legacy_invoice_snapshot, legacy_invoice_sha256=args.legacy_invoice_sha256,
    )
    def close_core_modules_for_recovery() -> None:
        """Maintenance boundary: no Core SQLite connection survives a generation switch."""
        if invoice_draft_service is not None:
            invoice_draft_service.store.close()
        if invoice_source is not None:
            invoice_source.close()
        if awb_explorer is not None:
            awb_explorer.close()
        if order_explorer is not None:
            order_explorer.close()
        if customer_price_module is not None:
            customer_price_module.close()
        if customer_explorer is not None:
            customer_explorer.close()
        item_explorer.close()

    try:
        handler_kwargs = {}
        source_invoice_explorer = item_explorer.source_invoice_explorer
        if type(source_invoice_explorer) is SourceInvoiceExplorer:
            handler_kwargs["source_invoice_explorer"] = source_invoice_explorer
        if args.legacy_invoice_snapshot is not None:
            # Verified legacy-invoice mode must never force the shared staff picker to
            # migrate/seed Core SQLite; mutation authority still requires it separately.
            handler_kwargs["identity_staff_provider"] = ActiveStaffProvider(CURRENT_IDENTITY_STAFF)
        server = ThreadingHTTPServer((args.host, args.port), make_handler(
            item_explorer, manifest, customer_explorer, customer_price_module, order_explorer, awb_explorer,
            invoice_source=invoice_source, invoice_draft_service=invoice_draft_service,
            accepted_snapshot_sha256=manifest["accepted_artifact_sha256"],
            customer_lan_ingress=args.container_ingress,
            allowed_mutation_origins=mutation_origins,
            recovery_authorizer=recovery_authorizer, recovery_service=recovery_service,
            recovery_maintenance=close_core_modules_for_recovery if recovery_service is not None else None,
            **handler_kwargs,
        ))
        print(f"APC Core Item Explorer listening on http://127.0.0.1:{args.port}")
        server.serve_forever()
    finally:
        close_core_modules_for_recovery()


if __name__ == "__main__":
    main()

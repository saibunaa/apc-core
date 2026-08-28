import argparse
import hashlib
import json
import os
import sqlite3
import stat
from http.server import ThreadingHTTPServer
from pathlib import Path

from .item_explorer import ItemExplorer, make_handler
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
        customer_explorer.backfill_from_snapshot()
        price_module = CustomerPriceModule.from_open_descriptor(descriptor, artifact_path, data_dir=data_dir)
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


def allowed_mutation_origins(*, container_ingress: bool) -> frozenset[str] | None:
    configured = os.environ.get("APC_CORE_ALLOWED_MUTATION_ORIGINS", "")
    origins = frozenset(origin.strip() for origin in configured.split(",") if origin.strip())
    if container_ingress and not origins:
        raise RuntimeContractError("container ingress requires explicit approved Program mutation origin")
    return origins or None


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
    item_explorer, customer_explorer, customer_price_module, manifest = load_accepted_customer_price_runtime(args.manifest, data_dir=data_dir)
    def close_core_modules_for_recovery() -> None:
        """Maintenance boundary: no Core SQLite connection survives a generation switch."""
        customer_price_module.close()
        customer_explorer.close()
        item_explorer.close()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(
        item_explorer, manifest, customer_explorer, customer_price_module, customer_lan_ingress=args.container_ingress,
        allowed_mutation_origins=mutation_origins,
        recovery_authorizer=recovery_authorizer, recovery_service=recovery_service,
        recovery_maintenance=close_core_modules_for_recovery if recovery_service is not None else None,
    ))
    print(f"APC Core Item Explorer listening on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

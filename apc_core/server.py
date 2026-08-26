import argparse
import hashlib
import json
import os
import sqlite3
import stat
from http.server import ThreadingHTTPServer
from pathlib import Path

from .item_explorer import ItemExplorer, make_handler


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Loopback-only APC Core Item Explorer pilot")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--port", default=8769, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--container-ingress", action="store_true", help="allow container-network ingress; never publish a host port")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not (args.container_ingress and args.host == "0.0.0.0"):
        parser.error("host must be loopback-only unless explicit container ingress is used")
    data_dir = Path(os.environ["APC_CORE_DATA_DIR"]) if os.environ.get("APC_CORE_DATA_DIR") else None
    explorer, manifest = load_accepted_runtime(args.manifest, data_dir=data_dir)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(explorer, manifest))
    print(f"APC Core Item Explorer listening on http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

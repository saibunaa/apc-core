"""Confirm-gated local APC Core snapshot import CLI."""
import argparse
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .item_explorer import ItemExplorer
from .snapshot_contract import SnapshotContractError, certify_snapshot_descriptor
from .server import RuntimeContractError, load_accepted_runtime

_DEFAULT_ROOT = Path("/home/sai/services/apc-program-preview/nas_staging_builds")
_NAME = re.compile(r"^apc_mdb_snapshot_\d{8}_\d{6}_bkk\.sqlite$")


@dataclass
class SelectedSnapshot:
    """A regular local staging snapshot retained by descriptor until close()."""

    descriptor: int
    path: Path

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> "SelectedSnapshot":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_snapshot_root(snapshot_root: Path) -> tuple[int, Path]:
    root = _absolute_path(Path(snapshot_root))
    try:
        visible = os.stat(root, follow_symlinks=False)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("snapshot root is not a directory") from error
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(visible.st_mode) or not stat.S_ISDIR(opened.st_mode) or (visible.st_dev, visible.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(descriptor)
        raise ValueError("snapshot root is not a directory")
    return descriptor, root


def open_latest_snapshot(snapshot_root: Path) -> SelectedSnapshot:
    """Select and hold the newest regular local staging snapshot without following names."""
    root_descriptor, root = _open_snapshot_root(snapshot_root)
    try:
        names = [name for name in os.listdir(root_descriptor) if _NAME.fullmatch(name)]
        candidates = []
        for name in names:
            try:
                entry = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(entry.st_mode):
                candidates.append(name)
        if not candidates:
            raise ValueError("no local staging snapshot found")
        name = max(candidates)
        expected = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_descriptor)
        opened = os.fstat(descriptor)
        visible_root = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISDIR(visible_root.st_mode)
            or (visible_root.st_dev, visible_root.st_ino) != (os.fstat(root_descriptor).st_dev, os.fstat(root_descriptor).st_ino)
        ):
            os.close(descriptor)
            raise ValueError("newest local staging snapshot changed during selection")
        return SelectedSnapshot(descriptor, root / name)
    finally:
        os.close(root_descriptor)


def latest_snapshot(snapshot_root: Path) -> Path:
    """Compatibility helper; import paths must use open_latest_snapshot()."""
    with open_latest_snapshot(snapshot_root) as selected:
        return selected.path


def preview_selected(selected: SelectedSnapshot) -> dict:
    explorer = ItemExplorer.from_open_descriptor(selected.descriptor, selected.path)
    try:
        rows = explorer._baseline_items()
        ids = [row["item_id"] for row in rows]
        duplicate_ids = {item_id for item_id in ids if item_id and ids.count(item_id) > 1}
        invalid = sum(bool(explorer._backfill_invalid_fields(row)) for row in rows if row["item_id"] and row["item_id"] not in duplicate_ids)
        auxiliary_conflicts = sum("_auxiliary_conflict" in row for row in rows)
        fields = sorted(field for field in explorer._baseline_items()[0] if field in rows[0] and any(row.get(field) for row in rows))
        return {"source": str(selected.path), "item_count": len(rows), "fields_available": fields,
                "duplicate_rows": sum(item_id in duplicate_ids for item_id in ids), "auxiliary_conflicts": auxiliary_conflicts,
                "out_of_range_rows": invalid}
    finally:
        explorer.close()


def preview(source: Path) -> dict:
    """Preview a caller path through a held no-follow regular-file descriptor."""
    source = _absolute_path(Path(source))
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError
    except OSError as error:
        raise ValueError("snapshot source is missing") from error
    with SelectedSnapshot(descriptor, source) as selected:
        return preview_selected(selected)


def import_latest(snapshot_root: Path, state_dir: Path) -> dict:
    with open_latest_snapshot(snapshot_root) as selected:
        report = preview_selected(selected)
        manifest_path = Path(state_dir) / "accepted_snapshot.json"
        certify_snapshot_descriptor(selected.descriptor, selected.path, manifest_path,
                                   datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    explorer, manifest = load_accepted_runtime(manifest_path, data_dir=state_dir)
    try:
        report.update({"confirmed": True, "accepted_sha256": manifest["accepted_artifact_sha256"], "backfill": explorer.backfill_from_snapshot()})
    finally:
        explorer.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or confirm import of newest local APC staging snapshot")
    parser.add_argument("--snapshot-root", type=Path, default=_DEFAULT_ROOT)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--confirm", action="store_true", help="certify and backfill after preview")
    args = parser.parse_args()
    try:
        if args.confirm:
            result = import_latest(args.snapshot_root, args.state_dir)
        else:
            with open_latest_snapshot(args.snapshot_root) as selected:
                result = preview_selected(selected)
    except (OSError, ValueError, SnapshotContractError, RuntimeContractError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

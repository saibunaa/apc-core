import argparse
from datetime import UTC, datetime
from pathlib import Path

from .snapshot_contract import certify_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="Certify a copied local SQLite snapshot for APC Core read-only use")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--customer-ready", action="store_true", help="require Customer source columns and declare customer runtime readiness")
    args = parser.parse_args()
    manifest = certify_snapshot(args.source, args.output, datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"), customer_ready=args.customer_ready)
    print(f"accepted item_count={manifest['item_count']} source_sha256={manifest['source_sha256']}")


if __name__ == "__main__":
    main()

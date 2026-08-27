"""Core-owned customer-item price rows backed by one immutable accepted snapshot.

Price Type is intentionally absent: it remains raw customer metadata and is never a
price input, derivation, or write target.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

from apc_core.item_explorer import CoreStore, display_text


_PRICE_TABLE = "MainDB__CUST_PRC"
_CUSTOMER_TABLE = "MainDB__CUST"
_ITEM_TABLE = "MainDB__ITEM"
_REQUIRED_PRICE_COLUMNS = {"Cust ID", "Item ID", "Price"}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _clean_code(value: object) -> str:
    return display_text(value).strip()


def _validate_price(value: object) -> str:
    if type(value) is not str:
        raise ValueError("invalid price")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 100:
        raise ValueError("invalid price")
    try:
        parsed = Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError("invalid price") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("invalid price")
    return cleaned


class CustomerPriceModule:
    """Read accepted source prices, retain Core-owned overrides, and audit mutations."""

    def __init__(self, source_path: Path, *, data_dir: Path | None = None, source_descriptor: int | None = None):
        self.source_path = Path(source_path)
        descriptor = os.dup(source_descriptor) if source_descriptor is not None else os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("price source must be a regular SQLite file")
            self._source = sqlite3.connect(
                f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True, check_same_thread=False
            )
            self._source.execute("PRAGMA query_only=ON")
            digest = hashlib.sha256()
            with os.fdopen(os.dup(descriptor), "rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            self._source_sha256 = digest.hexdigest()
        finally:
            os.close(descriptor)
        if not _REQUIRED_PRICE_COLUMNS.issubset(_table_columns(self._source, _PRICE_TABLE)):
            self._source.close()
            raise ValueError("price source lacks required CUST_PRC columns")
        if not {"Cust ID"}.issubset(_table_columns(self._source, _CUSTOMER_TABLE)):
            self._source.close()
            raise ValueError("price source lacks customers")
        if not {"Item ID"}.issubset(_table_columns(self._source, _ITEM_TABLE)):
            self._source.close()
            raise ValueError("price source lacks items")
        self._lock = threading.RLock()
        self._store = CoreStore(Path(data_dir or os.environ.get("APC_CORE_DATA_DIR", "state")))
        connection = self._store.connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS customer_price_rows ("
            "customer_code TEXT NOT NULL, item_id TEXT NOT NULL, item_description TEXT NOT NULL DEFAULT '', price TEXT NOT NULL, "
            "source_artifact_path TEXT NOT NULL, source_artifact_sha256 TEXT NOT NULL, "
            "imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, core_override INTEGER NOT NULL DEFAULT 0 "
            "CHECK(core_override IN (0,1)), PRIMARY KEY(customer_code,item_id))"
        )
        if "item_description" not in _table_columns(connection, "customer_price_rows"):
            connection.execute("ALTER TABLE customer_price_rows ADD COLUMN item_description TEXT NOT NULL DEFAULT ''")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS customer_price_quarantine ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, customer_code TEXT NOT NULL, item_id TEXT NOT NULL, "
            "reason TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS customer_price_activity ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, customer_code TEXT NOT NULL, item_id TEXT NOT NULL, "
            "action TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, "
            "actor_username TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.commit()

    @classmethod
    def from_open_descriptor(cls, descriptor: int, source_path: Path, *, data_dir: Path | None = None) -> "CustomerPriceModule":
        return cls(source_path, data_dir=data_dir, source_descriptor=descriptor)

    def close(self) -> None:
        self._source.close()
        self._store.close()

    def _catalog(self) -> tuple[set[str], dict[str, str]]:
        customers = {_clean_code(row[0]) for row in self._source.execute(f'SELECT "Cust ID" FROM "{_CUSTOMER_TABLE}"')}
        item_columns = _table_columns(self._source, _ITEM_TABLE)
        description_column = "Description" if "Description" in item_columns else None
        query = 'SELECT "Item ID"' + (', "Description"' if description_column else '') + f' FROM "{_ITEM_TABLE}"'
        items = {
            _clean_code(row[0]): _clean_code(row[1]) if description_column else ""
            for row in self._source.execute(query)
            if _clean_code(row[0])
        }
        customers.discard("")
        return customers, items

    def _source_rows(self) -> list[tuple[str, str, str]]:
        return [
            (_clean_code(customer), _clean_code(item), display_text(price).strip())
            for customer, item, price in self._source.execute(
                f'SELECT "Cust ID", "Item ID", "Price" FROM "{_PRICE_TABLE}"'
            )
        ]

    def _audit(self, customer_code: str, item_id: str, action: str, before: dict, after: dict, actor: str) -> None:
        self._store.connection.execute(
            "INSERT INTO customer_price_activity(customer_code,item_id,action,before_json,after_json,actor_username) VALUES (?,?,?,?,?,?)",
            (customer_code, item_id, action, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), actor),
        )

    def import_from_snapshot(self) -> dict[str, int]:
        """Import only unique, referentially valid snapshot rows; source remains read-only."""
        with self._lock, self._store.connection:
            connection = self._store.connection
            connection.execute("UPDATE customer_price_quarantine SET active=0 WHERE active=1")
            customers, items = self._catalog()
            rows = self._source_rows()
            keys = [(customer, item) for customer, item, _ in rows]
            duplicate_keys = {key for key, count in Counter(keys).items() if count > 1}
            counts = {"accepted": 0, "duplicate": 0, "unknown": 0, "preserved": 0}
            for customer_code, item_id, price in rows:
                if (customer_code, item_id) in duplicate_keys:
                    connection.execute(
                        "INSERT INTO customer_price_quarantine(customer_code,item_id,reason,active) VALUES (?,?,?,1)",
                        (customer_code, item_id, "duplicate_natural_key"),
                    )
                    counts["duplicate"] += 1
                    continue
                if customer_code not in customers:
                    connection.execute(
                        "INSERT INTO customer_price_quarantine(customer_code,item_id,reason,active) VALUES (?,?,?,1)",
                        (customer_code, item_id, "unknown_customer"),
                    )
                    counts["unknown"] += 1
                    continue
                if item_id not in items:
                    connection.execute(
                        "INSERT INTO customer_price_quarantine(customer_code,item_id,reason,active) VALUES (?,?,?,1)",
                        (customer_code, item_id, "unknown_item"),
                    )
                    counts["unknown"] += 1
                    continue
                try:
                    _validate_price(price)
                except ValueError:
                    connection.execute(
                        "INSERT INTO customer_price_quarantine(customer_code,item_id,reason,active) VALUES (?,?,?,1)",
                        (customer_code, item_id, "invalid_price"),
                    )
                    counts["unknown"] += 1
                    continue
                existing = connection.execute(
                    "SELECT price,core_override FROM customer_price_rows WHERE customer_code=? AND item_id=?",
                    (customer_code, item_id),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO customer_price_rows(customer_code,item_id,item_description,price,source_artifact_path,source_artifact_sha256,core_override) VALUES (?,?,?,?,?,?,0)",
                        (customer_code, item_id, items[item_id], price, str(self.source_path), self._source_sha256),
                    )
                elif existing[1]:
                    counts["preserved"] += 1
                else:
                    connection.execute(
                        "UPDATE customer_price_rows SET item_description=?,price=?,source_artifact_path=?,source_artifact_sha256=?,imported_at=CURRENT_TIMESTAMP WHERE customer_code=? AND item_id=?",
                        (items[item_id], price, str(self.source_path), self._source_sha256, customer_code, item_id),
                    )
                counts["accepted"] += 1
            return counts

    def customer_codes(self) -> list[str]:
        return [row[0] for row in self._store.connection.execute("SELECT DISTINCT customer_code FROM customer_price_rows ORDER BY customer_code")]

    def _require_customer(self, customer_code: object) -> str:
        if type(customer_code) is not str or not customer_code.strip():
            raise ValueError("invalid customer code")
        clean = customer_code.strip()
        if not self._store.connection.execute("SELECT 1 FROM customer_price_rows WHERE customer_code=?", (clean,)).fetchone():
            raise ValueError("unknown customer code")
        return clean

    def search(self, customer_code: object, query: object = "", limit: int = 100, offset: int = 0) -> dict[str, object]:
        customer = self._require_customer(customer_code)
        if type(query) is not str:
            raise ValueError("invalid query")
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        term = query.strip().casefold()
        rows = [
            {
                "customer_code": row[0], "item_id": row[1], "item_description": row[2], "price": row[3],
                "source_artifact_sha256": row[4], "provenance": "core_override" if row[5] else "snapshot",
            }
            for row in self._store.connection.execute(
                "SELECT customer_code,item_id,item_description,price,source_artifact_sha256,core_override FROM customer_price_rows WHERE customer_code=? ORDER BY item_id",
                (customer,),
            )
        ]
        if term:
            rows = [row for row in rows if term in row["item_id"].casefold() or term in row["item_description"].casefold()]
        return {"customer_code": customer, "total": len(rows), "limit": limit, "offset": offset, "rows": rows[offset:offset + limit]}

    def edit(self, customer_code: object, item_id: object, price: object, actor_username: object) -> dict[str, object]:
        customer = self._require_customer(customer_code)
        if type(item_id) is not str or not item_id.strip():
            raise ValueError("invalid item")
        item = item_id.strip()
        clean_price = _validate_price(price)
        actor = self._store.require_active_actor(actor_username)
        with self._lock, self._store.connection:
            row = self._store.connection.execute(
                "SELECT price,source_artifact_sha256 FROM customer_price_rows WHERE customer_code=? AND item_id=?",
                (customer, item),
            ).fetchone()
            if row is None:
                raise ValueError("unknown customer-item price row")
            before = {"price": row[0]}
            after = {"price": clean_price}
            self._store.connection.execute(
                "UPDATE customer_price_rows SET price=?,core_override=1 WHERE customer_code=? AND item_id=?",
                (clean_price, customer, item),
            )
            self._audit(customer, item, "price_edited", before, after, actor)
        return {"customer_code": customer, "item_id": item, "price": clean_price, "source_artifact_sha256": row[1], "provenance": "core_override"}

    def preview_tsv(self, customer_code: object, tsv: object) -> dict[str, object]:
        customer = self._require_customer(customer_code)
        if type(tsv) is not str or len(tsv.encode("utf-8")) > 200_000:
            raise ValueError("invalid TSV")
        lines = [line for line in tsv.splitlines() if line.strip()]
        if not lines:
            raise ValueError("empty TSV")
        header = [cell.strip().casefold() for cell in lines[0].split("\t")]
        if header != ["item id", "price"]:
            raise ValueError("TSV must have Item ID and Price columns")
        known_items = {row[0] for row in self._store.connection.execute("SELECT item_id FROM customer_price_rows WHERE customer_code=?", (customer,))}
        valid: list[dict[str, object]] = []
        invalid: list[dict[str, object]] = []
        unknown: list[dict[str, object]] = []
        duplicate: list[dict[str, object]] = []
        changes: list[dict[str, str]] = []
        seen: set[str] = set()
        current = {row[0]: row[1] for row in self._store.connection.execute("SELECT item_id,price FROM customer_price_rows WHERE customer_code=?", (customer,))}
        for line_number, line in enumerate(lines[1:], start=2):
            cells = line.split("\t")
            if len(cells) != 2:
                invalid.append({"line": line_number, "reason": "expected_two_columns"})
                continue
            item, raw_price = cells[0].strip(), cells[1].strip()
            if not item:
                invalid.append({"line": line_number, "reason": "blank_item_id"})
                continue
            if item in seen:
                duplicate.append({"line": line_number, "item_id": item, "reason": "duplicate_paste_key"})
                continue
            seen.add(item)
            if item not in known_items:
                unknown.append({"line": line_number, "item_id": item, "reason": "unknown_customer_item_row"})
                continue
            try:
                price = _validate_price(raw_price)
            except ValueError:
                invalid.append({"line": line_number, "item_id": item, "reason": "invalid_price"})
                continue
            entry = {"line": line_number, "customer_code": customer, "item_id": item, "price": price}
            valid.append(entry)
            if current[item] != price:
                changes.append({"customer_code": customer, "item_id": item, "before": current[item], "after": price})
        return {"customer_code": customer, "valid": valid, "invalid": invalid, "unknown": unknown, "duplicate": duplicate, "changes": changes}

    def apply_preview(self, customer_code: object, preview: object, actor_username: object) -> dict[str, int]:
        customer = self._require_customer(customer_code)
        if type(preview) is not dict or preview.get("customer_code") != customer:
            raise ValueError("invalid preview")
        if any(preview.get(key) for key in ("invalid", "unknown", "duplicate")) or type(preview.get("valid")) is not list:
            raise ValueError("preview requires correction")
        actor = self._store.require_active_actor(actor_username)
        with self._lock, self._store.connection:
            applied = 0
            for row in preview["valid"]:
                if type(row) is not dict:
                    raise ValueError("invalid preview")
                item = row.get("item_id")
                price = row.get("price")
                if type(item) is not str:
                    raise ValueError("invalid preview")
                clean_price = _validate_price(price)
                current = self._store.connection.execute(
                    "SELECT price FROM customer_price_rows WHERE customer_code=? AND item_id=?", (customer, item)
                ).fetchone()
                if current is None:
                    raise ValueError("unknown customer-item price row")
                if current[0] == clean_price:
                    continue
                self._store.connection.execute(
                    "UPDATE customer_price_rows SET price=?,core_override=1 WHERE customer_code=? AND item_id=?",
                    (clean_price, customer, item),
                )
                self._audit(customer, item, "price_bulk_applied", {"price": current[0]}, {"price": clean_price}, actor)
                applied += 1
        return {"applied": applied}

    def html(self) -> str:
        """Static shell; all snapshot values are rendered through textContent."""
        return """<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>APC Core · Customer Price</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57;--warn:#8a6417}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif}.shell{max-width:1240px;margin:auto;padding:26px}.back{color:var(--accent);font-weight:700;text-decoration:none}.toolbar{position:sticky;top:0;z-index:2;display:grid;grid-template-columns:minmax(220px,.5fr) minmax(260px,1fr) auto;gap:9px;padding:12px 0;background:var(--cream)}input,select,textarea,button{font:inherit;border:1px solid var(--line);border-radius:10px;padding:10px;background:#fffdfa}button{cursor:pointer;font-weight:700}.primary{background:var(--accent);color:#fff;border-color:var(--accent)}.workspace{border:1px solid var(--line);border-radius:18px;background:var(--paper);overflow:hidden}.hint{margin:0;color:var(--muted)}.summary{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;gap:12px;justify-content:space-between}.rows{width:100%;border-collapse:collapse}.rows th,.rows td{padding:12px 14px;border-bottom:1px solid var(--line);text-align:left}.rows th{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}.rows input{width:110px;padding:7px}.provenance{font-size:12px;color:var(--muted)}.paste{margin-top:18px;padding:16px;border:2px solid #d7c98d;border-radius:14px;background:#fffaf0}.soft-brutalist{box-shadow:4px 4px 0 #d7c98d}.paste textarea{display:block;width:100%;min-height:126px;margin:10px 0;resize:vertical}.preview{margin-top:12px;display:grid;gap:8px}.preview-card{padding:9px 11px;border-left:4px solid var(--warn);background:#fff}.preview-card.good{border-color:var(--accent)}[hidden]{display:none!important}@media(max-width:720px){.shell{padding:14px}.toolbar{grid-template-columns:1fr}.summary{display:block}.rows{font-size:12px}.rows th,.rows td{padding:9px 7px}}</style><body><main class=\"shell\"><a class=\"back\" href=\"../\">← APC Core</a><h1>Customer Price</h1><p class=\"hint\">Select a Customer Code, then search and edit that customer’s imported item-price rows. No pricing formulas or customer metadata rules are used.</p><section class=\"toolbar\"><label>Customer Code <select id=\"customer-code\"><option value=\"\">Choose customer</option></select></label><label>Search item-price rows <input id=\"search\" type=\"search\" autocomplete=\"off\"></label><button id=\"reload\" type=\"button\">Reload</button></section><section class=\"workspace\"><div class=\"summary\"><strong id=\"status\">Choose a Customer Code</strong><span class=\"provenance\">Accepted snapshot provenance retained per row</span></div><table class=\"rows\"><thead><tr><th>Item ID</th><th>Item</th><th>Price</th><th>Provenance</th><th></th></tr></thead><tbody id=\"rows\"></tbody></table></section><section class=\"paste soft-brutalist\"><h2>Paste Excel TSV</h2><p class=\"hint\">Header must be <b>Item ID</b> and <b>Price</b>. Preview classifies valid, invalid, unknown and duplicate rows before Apply.</p><textarea id=\"tsv\" aria-label=\"Paste Excel TSV\" placeholder=\"Item ID&#9;Price\"></textarea><button id=\"preview\" type=\"button\">Preview</button> <button id=\"apply\" class=\"primary\" type=\"button\" disabled>Apply</button><div id=\"preview-result\" class=\"preview\" aria-live=\"polite\"></div></section></main><script>(()=>{const $=id=>document.getElementById(id),customer=$(\"customer-code\"),search=$(\"search\"),rows=$(\"rows\"),status=$(\"status\"),tsv=$(\"tsv\"),previewResult=$(\"preview-result\"),apply=$(\"apply\");let preview=null;function active(){return window.apcCoreActiveStaff||\"\"}function request(path,method=\"GET\",body){return fetch(path,{method,headers:body?{\"Content-Type\":\"application/json\"}:{},body:body?JSON.stringify(body):undefined}).then(async response=>{const payload=await response.json();if(!response.ok)throw Error(payload.error||\"Request failed\");return payload})}function text(tag,value){const node=document.createElement(tag);node.textContent=value;return node}function clear(node){node.replaceChildren()}function render(payload){clear(rows);status.textContent=payload.total+\" item-price rows\";for(const row of payload.rows){const tr=document.createElement(\"tr\"),price=document.createElement(\"input\"),save=document.createElement(\"button\");price.value=row.price;price.inputMode=\"decimal\";save.type=\"button\";save.textContent=\"Save\";save.onclick=async()=>{if(!active()){status.textContent=\"Choose a user before saving.\";return}try{await request(\"api/customers/\"+encodeURIComponent(customer.value)+\"/items/\"+encodeURIComponent(row.item_id),\"POST\",{price:price.value,actor:active()});load()}catch(error){status.textContent=error.message}};tr.append(text(\"td\",row.item_id),text(\"td\",row.item_description),text(\"td\",\"\"));tr.children[2].append(price);tr.append(text(\"td\",row.provenance),text(\"td\",\"\"));tr.children[4].append(save);rows.append(tr)}}function load(){preview=null;apply.disabled=true;if(!customer.value){clear(rows);status.textContent=\"Choose a Customer Code\";return}request(\"api/customers/\"+encodeURIComponent(customer.value)+\"?q=\"+encodeURIComponent(search.value)).then(render).catch(error=>status.textContent=error.message)}function renderPreview(result){preview=result;clear(previewResult);for(const [kind,values] of Object.entries({valid:result.valid,invalid:result.invalid,unknown:result.unknown,duplicate:result.duplicate,changes:result.changes})){const card=document.createElement(\"div\");card.className=\"preview-card\"+(kind===\"valid\"||kind===\"changes\"?\" good\":\"\");card.textContent=kind+\": \"+values.length;previewResult.append(card)}apply.disabled=!(result.valid.length||result.changes.length)||result.invalid.length>0||result.unknown.length>0||result.duplicate.length>0}request(\"api/customers\").then(payload=>{for(const code of payload.customer_codes){const option=document.createElement(\"option\");option.value=code;option.textContent=code;customer.append(option)}});customer.onchange=load;search.oninput=()=>{clearTimeout(search.timer);search.timer=setTimeout(load,160)};$(\"reload\").onclick=load;$(\"preview\").onclick=()=>{if(!customer.value){status.textContent=\"Choose a Customer Code\";return}request(\"api/customers/\"+encodeURIComponent(customer.value)+\"/paste/preview\",\"POST\",{tsv:tsv.value}).then(renderPreview).catch(error=>{preview=null;apply.disabled=true;status.textContent=error.message})};apply.onclick=()=>{if(!active()){status.textContent=\"Choose a user before Apply.\";return}if(!preview)return;request(\"api/customers/\"+encodeURIComponent(customer.value)+\"/paste/apply\",\"POST\",{tsv:tsv.value,actor:active()}).then(payload=>{status.textContent=payload.applied+\" rows applied\";load()}).catch(error=>status.textContent=error.message)};window.addEventListener(\"apc-core-identity\",()=>{})})()</script></body></html>"""

    def activity(self, customer_code: object) -> list[dict[str, object]]:
        customer = self._require_customer(customer_code)
        rows = self._store.connection.execute(
            "SELECT customer_code,item_id,action,before_json,after_json,actor_username FROM customer_price_activity WHERE customer_code=? ORDER BY id",
            (customer,),
        )
        return [
            {"customer_code": row[0], "item_id": row[1], "action": row[2], "before": json.loads(row[3]), "after": json.loads(row[4]), "actor_username": row[5]}
            for row in rows
        ]

    def quarantine(self) -> list[dict[str, str]]:
        return [
            {"customer_code": row[0], "item_id": row[1], "reason": row[2]}
            for row in self._store.connection.execute(
                "SELECT customer_code,item_id,reason FROM customer_price_quarantine WHERE active=1 ORDER BY id"
            )
        ]

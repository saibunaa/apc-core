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
import secrets
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
        self._previews: dict[str, dict[str, object]] = {}
        self._store = CoreStore(Path(data_dir or os.environ.get("APC_CORE_DATA_DIR", "state")))
        connection = self._store.connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS customer_price_rows ("
            "customer_code TEXT NOT NULL, item_id TEXT NOT NULL, item_description TEXT NOT NULL DEFAULT '', price TEXT NOT NULL, "
            "source_artifact_path TEXT NOT NULL, source_artifact_sha256 TEXT NOT NULL, "
            "imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, core_override INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1 "
            "CHECK(core_override IN (0,1)), CHECK(active IN (0,1)), PRIMARY KEY(customer_code,item_id))"
        )
        columns = _table_columns(connection, "customer_price_rows")
        if "item_description" not in columns:
            connection.execute("ALTER TABLE customer_price_rows ADD COLUMN item_description TEXT NOT NULL DEFAULT ''")
        if "active" not in columns:
            connection.execute("ALTER TABLE customer_price_rows ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))")
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
            connection.execute("UPDATE customer_price_rows SET active=0 WHERE active=1")
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
                    connection.execute(
                        "UPDATE customer_price_rows SET item_description=?,source_artifact_path=?,source_artifact_sha256=?,imported_at=CURRENT_TIMESTAMP,active=1 WHERE customer_code=? AND item_id=?",
                        (items[item_id], str(self.source_path), self._source_sha256, customer_code, item_id),
                    )
                    counts["preserved"] += 1
                else:
                    connection.execute(
                        "UPDATE customer_price_rows SET item_description=?,price=?,source_artifact_path=?,source_artifact_sha256=?,imported_at=CURRENT_TIMESTAMP,active=1 WHERE customer_code=? AND item_id=?",
                        (items[item_id], price, str(self.source_path), self._source_sha256, customer_code, item_id),
                    )
                counts["accepted"] += 1
            return counts

    def customer_codes(self) -> list[str]:
        return [row[0] for row in self._store.connection.execute("SELECT DISTINCT customer_code FROM customer_price_rows WHERE active=1 ORDER BY customer_code")]

    def _require_customer(self, customer_code: object) -> str:
        if type(customer_code) is not str or not customer_code.strip():
            raise ValueError("invalid customer code")
        clean = customer_code.strip()
        if not self._store.connection.execute("SELECT 1 FROM customer_price_rows WHERE customer_code=? AND active=1", (clean,)).fetchone():
            raise ValueError("unknown customer code")
        return clean

    def search(self, customer_code: object, query: object = "", limit: int = 100, offset: int = 0) -> dict[str, object]:
        customer = self._require_customer(customer_code)
        if type(query) is not str:
            raise ValueError("invalid query")
        limit = max(1, min(int(limit), 250))
        offset = max(0, int(offset))
        term = query.strip().casefold()
        rows = [
            {
                "customer_code": row[0], "item_id": row[1], "item_description": row[2], "price": row[3],
                "source_artifact_sha256": row[4], "provenance": "core_override" if row[5] else "snapshot",
            }
            for row in self._store.connection.execute(
                "SELECT customer_code,item_id,item_description,price,source_artifact_sha256,core_override FROM customer_price_rows WHERE customer_code=? AND active=1 ORDER BY item_id",
                (customer,),
            )
        ]
        if term:
            rows = [row for row in rows if term in row["item_id"].casefold() or term in row["item_description"].casefold()]
        total = len(rows); next_offset = offset + limit; return {"customer_code": customer, "total": total, "limit": limit, "offset": offset, "has_more": next_offset < total, "next_offset": next_offset if next_offset < total else None, "rows": rows[offset:next_offset]}

    def edit(self, customer_code: object, item_id: object, price: object, actor_username: object) -> dict[str, object]:
        customer = self._require_customer(customer_code)
        if type(item_id) is not str or not item_id.strip():
            raise ValueError("invalid item")
        item = item_id.strip()
        clean_price = _validate_price(price)
        actor = self._store.require_active_actor(actor_username)
        with self._lock, self._store.connection:
            row = self._store.connection.execute(
                "SELECT price,source_artifact_sha256 FROM customer_price_rows WHERE customer_code=? AND item_id=? AND active=1",
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
        has_header = header == ["item id", "price"]
        data_lines = lines[1:] if has_header else lines
        data_start = 2 if has_header else 1
        known_items = {row[0] for row in self._store.connection.execute("SELECT item_id FROM customer_price_rows WHERE customer_code=? AND active=1", (customer,))}
        valid: list[dict[str, object]] = []
        invalid: list[dict[str, object]] = []
        unknown: list[dict[str, object]] = []
        duplicate: list[dict[str, object]] = []
        changes: list[dict[str, str]] = []
        seen: set[str] = set()
        current = {row[0]: row[1] for row in self._store.connection.execute("SELECT item_id,price FROM customer_price_rows WHERE customer_code=? AND active=1", (customer,))}
        for line_number, line in enumerate(data_lines, start=data_start):
            cells = line.split("\t") if "\t" in line else line.strip().split()
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
        result: dict[str, object] = {"customer_code": customer, "valid": valid, "invalid": invalid, "unknown": unknown, "duplicate": duplicate, "changes": changes}
        preview_id = secrets.token_urlsafe(24)
        with self._lock:
            self._previews[preview_id] = json.loads(json.dumps(result, ensure_ascii=False))
        result["preview_id"] = preview_id
        return result

    def apply_preview_id(self, customer_code: object, preview_id: object, actor_username: object) -> dict[str, int]:
        customer = self._require_customer(customer_code)
        if type(preview_id) is not str:
            raise ValueError("invalid preview")
        with self._lock:
            preview = self._previews.get(preview_id)
            if preview is None or preview.get("customer_code") != customer:
                raise ValueError("invalid preview")
            result = self._apply_preview(customer, preview, actor_username)
            del self._previews[preview_id]
            return result

    def _apply_preview(self, customer_code: object, preview: object, actor_username: object) -> dict[str, int]:
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
                    "SELECT price FROM customer_price_rows WHERE customer_code=? AND item_id=? AND active=1", (customer, item)
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
        """Keyboard-first static shell; snapshot values use textContent."""
        return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core · Customer Price</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#faf7f2;--paper:#fffdfa;--accent:#1d6b57;--warn:#8a6417;--list-alt:#f1ede4;--list-hover:#dcefe5}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1240px;margin:auto;padding:26px}.toolbar{display:grid;grid-template-columns:1fr 1fr auto auto auto;gap:9px;padding:12px 0}input,textarea,button{font:inherit;border:1px solid var(--line);border-radius:10px;padding:10px;background:#fffdfa}button{cursor:pointer;font-weight:700}button:disabled{opacity:.55;cursor:not-allowed}.primary{background:var(--accent);color:#fff}.workspace{border:1px solid var(--line);border-radius:18px;background:var(--paper);overflow:hidden}.hint{color:var(--muted)}.summary{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}.rows{width:100%;border-collapse:collapse}.rows th,.rows td{padding:12px 14px;border-bottom:1px solid var(--line);text-align:left}.rows th{font-size:11px;text-transform:uppercase;color:var(--muted)}.rows tbody tr:nth-child(even){background:var(--list-alt)}.rows tbody tr:hover,.rows tbody tr:focus-within{background:var(--list-hover);outline:2px solid #b9dbcf;outline-offset:-2px}.rows input{width:110px;padding:7px}.modal-backdrop{position:fixed;inset:0;z-index:5;display:grid;place-items:center;padding:18px;background:#24272b66}.modal{width:min(720px,100%);max-height:calc(100vh - 36px);overflow:auto;padding:20px;border:2px solid #d7c98d;border-radius:14px;background:#fffaf0;box-shadow:4px 4px 0 #d7c98d}.modal textarea{display:block;width:100%;min-height:180px;margin:10px 0}.modal-actions{display:flex;gap:9px;justify-content:flex-end}.preview{margin-top:12px;display:grid;gap:8px}.preview-card{padding:9px 11px;border-left:4px solid var(--warn);background:#fff}.preview-card.good{border-color:var(--accent)}[hidden]{display:none!important}@media(max-width:820px){.shell{padding:14px}.toolbar{grid-template-columns:1fr 1fr}.toolbar label{grid-column:span 2}.summary{display:block}}</style><body><main class="shell"><a class="back" href="../">Main menu</a><h1>Customer Price</h1><p class="hint">Keyboard-first customer prices. Choose a Customer Code, then search its imported item-price rows.</p><section class="toolbar"><label>Customer Code <input id="customer-code" list="customer-options" role="combobox" aria-autocomplete="list" aria-controls="customer-options" autocomplete="off" placeholder="Type a customer code"></label><datalist id="customer-options"></datalist><label>Search item-price rows <input id="search" type="search"></label><button id="reload" type="button">Reload</button><button id="edit" type="button" disabled>Edit prices</button><button id="bulk" type="button" disabled>Bulk edit</button></section><section class="workspace"><div class="summary"><strong id="status" aria-live="polite">Choose a Customer Code</strong></div><table class="rows"><thead><tr><th>Item ID</th><th>Item</th><th>Price</th><th id="row-action" hidden>Action</th></tr></thead><tbody id="rows"></tbody></table><button id="load-more" type="button" hidden>Load more</button></section></main><div id="bulk-dialog" class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="bulk-title" hidden><section class="modal"><h2 id="bulk-title">Bulk edit prices</h2><p class="hint">Paste <b>Item ID</b> then <b>Price</b>, tab-separated. A conventional header row is optional and is excluded automatically.</p><textarea id="tsv" aria-label="Paste Item ID and Price rows" placeholder="IT-001&#9;14.00"></textarea><div id="bulk-error" role="alert" aria-live="assertive" hidden></div><div class="modal-actions"><button id="close-bulk" type="button">Cancel</button><button id="preview" type="button">Preview</button><button id="apply" class="primary" type="button" disabled>Apply</button></div><div id="preview-result" class="preview" aria-live="polite"></div></section></div><script>(()=>{const $=id=>document.getElementById(id),customer=$("customer-code"),options=$("customer-options"),search=$("search"),rows=$("rows"),status=$("status"),edit=$("edit"),bulk=$("bulk"),dialog=$("bulk-dialog"),tsv=$("tsv"),result=$("preview-result"),apply=$("apply"),action=$("row-action"),loadMoreButton=$("load-more"),bulkError=$("bulk-error");let preview=null,editMode=false,lastFocus,lastPage=null,customerCodes=[],loadingMore=false,pendingFocusItem=null,dirtyEdits=new Set();const selected=()=>customer.value.trim(),active=()=>window.apcCoreActiveStaff||"";const request=(path,method="GET",body)=>fetch(path,{method,headers:body?{"Content-Type":"application/json"}:{},body:body?JSON.stringify(body):undefined}).then(async r=>{const p=await r.json();if(!r.ok)throw Error(p.error||"Request failed");return p});const text=(tag,value)=>{const n=document.createElement(tag);n.textContent=value;return n};const clear=n=>n.replaceChildren();function sync(){const ready=!!selected();edit.disabled=!ready;bulk.disabled=!ready;action.hidden=!editMode;edit.textContent=editMode?"Done editing":"Edit prices"}function render(p,append=false){lastPage=append&&lastPage?{...p,rows:[...lastPage.rows,...p.rows]}:p;if(!append)clear(rows);status.textContent=p.total+" item-price rows";for(const r of p.rows){const tr=document.createElement("tr"),price=text("td",r.price),cell=document.createElement("td");tr.append(text("td",r.item_id),text("td",r.item_description),price);if(editMode){const input=document.createElement("input"),save=document.createElement("button");input.id="price-"+r.item_id;input.value=r.price;input.oninput=()=>{dirtyEdits.add(r.item_id)};input.inputMode="decimal";input.setAttribute("aria-label","Price for "+r.item_id);save.textContent="Save";save.onclick=async()=>{if(!active()){status.textContent="Choose a user before saving.";return}try{await request("api/customers/"+encodeURIComponent(r.customer_code)+"/items/"+encodeURIComponent(r.item_id),"POST",{price:input.value,actor:active()});pendingFocusItem=r.item_id;dirtyEdits.delete(r.item_id);load()}catch(e){status.textContent=e.message}};price.replaceChildren(input);cell.append(save)}if(editMode)tr.append(cell);rows.append(tr)}if(pendingFocusItem){const focusItem=pendingFocusItem;requestAnimationFrame(()=>{$("price-"+focusItem)?.focus();pendingFocusItem=null})}loadMoreButton.hidden=!p.has_more;sync()}function load(offset=0,append=false){if(append&&loadingMore)return;if(!append&&dirtyEdits.size){if(!window.confirm("Discard unsaved price edits?"))return;dirtyEdits.clear()}if(append){loadingMore=true;loadMoreButton.disabled=true}const code=selected();if(!append){lastPage=null;preview=null;apply.disabled=true}if(!code){clear(rows);status.textContent="Choose a Customer Code";sync();return}request("api/customers/"+encodeURIComponent(code)+"?q="+encodeURIComponent(search.value)+"&limit=100&offset="+offset).then(p=>{if(selected()===code)render(p,append)}).catch(e=>{if(selected()===code){status.textContent=e.message;sync()}}).finally(()=>{if(append){loadingMore=false;loadMoreButton.disabled=false}})}function loadMore(){if(lastPage?.next_offset!==null)load(lastPage.next_offset,true)}function showPreview(p){preview=p;clear(result);for(const [kind,values] of Object.entries({valid:p.valid,invalid:p.invalid,unknown:p.unknown,duplicate:p.duplicate,changes:p.changes})){const card=document.createElement("div");card.className="preview-card"+(kind==="valid"||kind==="changes"?" good":"");card.append(text("strong",kind+": "+values.length));for(const r of values)card.append(text("div",kind==="changes"?r.item_id+": "+r.before+" → "+r.after:(r.item_id||"line "+r.line)+": "+(r.reason||r.price)));result.append(card)}apply.disabled=!(p.valid.length||p.changes.length)||p.invalid.length>0||p.unknown.length>0||p.duplicate.length>0}function showBulkError(message){bulkError.textContent=message;bulkError.hidden=false}function trapFocus(event){if(event.key!=="Tab")return;const focusable=[...dialog.querySelectorAll("textarea,button:not([disabled])")];if(!focusable.length)return;if(event.shiftKey&&document.activeElement===focusable[0]){event.preventDefault();focusable.at(-1).focus()}else if(!event.shiftKey&&document.activeElement===focusable.at(-1)){event.preventDefault();focusable[0].focus()}}function openBulk(){if(!selected()){status.textContent="Choose a Customer Code";customer.focus();return}lastFocus=bulk;bulkError.hidden=true;dialog.hidden=false;tsv.focus()}function closeBulk(){dialog.hidden=true;lastFocus?.focus()}request("api/customers").then(p=>{customerCodes=p.customer_codes||[];customerCodes.forEach(code=>{const o=document.createElement("option");o.value=code;options.append(o)})});tsv.oninput=()=>{preview=null;apply.disabled=true;bulkError.hidden=true;clear(result)};function commitCustomer(){const previousCustomer=lastPage?.customer_code||"",typed=selected().toLowerCase(),match=customerCodes.find(code=>code.toLowerCase()===typed)||customerCodes.find(code=>code.toLowerCase().startsWith(typed));const nextCustomer=match||selected();if(nextCustomer!==previousCustomer&&dirtyEdits.size){if(!window.confirm("Discard unsaved price edits?")){customer.value=previousCustomer;return}dirtyEdits.clear()}if(match)customer.value=match;load()}customer.addEventListener("change",commitCustomer);customer.addEventListener("keydown",e=>{if(e.key==="Enter"||(e.key==="Tab"&&!e.shiftKey)){e.preventDefault();commitCustomer();if(e.key==="Tab")setTimeout(()=>search.focus(),0)}if(e.key==="Escape"){const previousCustomer=lastPage?.customer_code||"";if(dirtyEdits.size){if(!window.confirm("Discard unsaved price edits?")){customer.value=previousCustomer;return}dirtyEdits.clear()}customer.value="";load()}});search.addEventListener("input",()=>{clearTimeout(search.timer);search.timer=setTimeout(load,160)});$("reload").onclick=load;edit.onclick=()=>{if(!selected())return;if(editMode&&dirtyEdits.size&&!window.confirm("Discard unsaved price edits?"))return;editMode=!editMode;dirtyEdits.clear();if(lastPage)render(lastPage);else load()};bulk.onclick=openBulk;loadMoreButton.onclick=loadMore;$("close-bulk").onclick=closeBulk;dialog.addEventListener("keydown",trapFocus);$("preview").onclick=()=>request("api/customers/"+encodeURIComponent(selected())+"/paste/preview","POST",{tsv:tsv.value}).then(showPreview).catch(e=>{preview=null;apply.disabled=true;showBulkError(e.message)});apply.onclick=()=>{if(!active()||!preview){showBulkError("Choose a user and preview before Apply.");return}request("api/customers/"+encodeURIComponent(selected())+"/paste/apply","POST",{preview_id:preview.preview_id,actor:active()}).then(p=>{status.textContent=p.applied+" rows applied";closeBulk();load()}).catch(e=>showBulkError(e.message))};document.addEventListener("keydown",e=>{if(e.key==="Escape"&&!dialog.hidden){e.preventDefault();closeBulk()}if((e.ctrlKey||e.metaKey)&&e.key==="Enter"&&!dialog.hidden){e.preventDefault();$("preview").click()}});sync();window.addEventListener("apc-core-identity",()=>{})})()</script></body></html>"""

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

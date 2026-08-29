"""Read-only A.W.B. / Shipment reader for an accepted SQLite snapshot.

The legacy ``AWB`` column names are recovered from VB6 ``rSQL![...]`` references
and have never been checked against a real accepted snapshot, so this module
resolves them by alias rather than asserting one exact spelling.  Only the three
identity columns are mandatory; every other field degrades to empty rather than
refusing the whole artifact.

Nothing here computes a freight rate.  The VB6 ``txtWeight_Change`` multi-match
branch has no ``MoveNext`` and loops forever; the rule is not ported in any form,
not even as a suggestion, and ``MainDB__FREIGHT`` is never opened.
"""

import hashlib
import os
import sqlite3
import stat
import threading
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


AWB_TABLE = "MainDB__AWB"
CUSTOMER_TABLE = "MainDB__CUST"
CUSTOMER_CONFIG_TABLE = "MainDB__CUST_CON"

# Identity is mandatory: money is never shown beside a record Core cannot name.
_IDENTITY_FIELDS = ("invoice_no", "awb_no", "awb_date")

_AWB_COLUMNS = {
    "invoice_no": ("Inv No", "INVOICE.Inv No", "Invoice No", "InvNo"),
    "awb_no": ("AWB", "AWB No", "AWBNo"),
    "awb_date": ("AWB Date", "AWBDate", "Date"),
    "ship_by": ("shipby", "Ship By", "ShipBy", "Flight"),
    "boxes": ("AWB Box", "Box", "AWBBox"),
    "destination": ("Province", "Destination", "Airport"),
    "weight": ("Weight",),
    "rate": ("RATE", "Rate"),
    "agent": ("Agent",),
    "carrier": ("Carrier",),
    "total_thb": ("Total THB", "TotalTHB"),
    "total_us": ("Total US", "TotalUS", "Total US$"),
    "exrate": ("exRate", "ExRate", "Ex Rate"),
}
_CUSTOMER_COLUMNS = {"customer_id": ("Cust ID", "Customer ID"), "customer_name": ("Name",)}
# Correction carried from the 2026-08-29 review: customer_explorer already
# alias-reads Charges from this table, so the per-customer cargo charge is
# available today wherever the export preserved it.
_CONFIG_COLUMNS = {"customer_id": ("Cust ID", "Customer ID"), "charges": ("Charges",)}

# Legacy evidence, not policy.  Named on screen wherever it is applied.
LEGACY_ANOMALY_BAND = (Decimal("30"), Decimal("37"))
LEGACY_ANOMALY_ORIGIN = "frmAWBList Form_Load, 2000s-era build — not confirmed as current policy"

_MAX_PAGE = 250
_CENTS = Decimal("0.01")


class ReadOnlySourceContractError(ValueError):
    """The accepted source is not the closed, read-only AWB schema."""


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _number(value: object) -> Decimal | None:
    """Legacy numerics arrive as text, blanks and stray separators alike."""
    raw = _text(value).replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _line(label: str, unit: str, stored: Decimal | None, recomputed: Decimal | None, source: str, flags: list[str] | None = None) -> dict[str, object]:
    """One row of the provenance ledger.  Stored and recomputed never merge."""
    agrees = None
    delta = None
    if stored is not None and recomputed is not None:
        agrees = stored.quantize(_CENTS) == recomputed.quantize(_CENTS)
        if not agrees:
            delta = _money(abs(stored - recomputed))
    return {
        "label": label,
        "unit": unit,
        "stored": _money(stored),
        "recomputed": _money(recomputed),
        "agrees": agrees,
        "delta": delta,
        "source": source,
        "flags": flags or [],
    }


class AWBExplorer:
    """Bounded, immutable reader for accepted shipment data only."""

    def __init__(self, source_path: Path):
        self.source_path = Path(source_path)
        descriptor = os.open(self.source_path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self._initialize_from_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def from_open_descriptor(cls, descriptor: int, source_path: Path) -> "AWBExplorer":
        explorer = cls.__new__(cls)
        explorer.source_path = Path(source_path)
        explorer._initialize_from_descriptor(descriptor)
        return explorer

    def _initialize_from_descriptor(self, descriptor: int) -> None:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReadOnlySourceContractError("awb explorer source must be a regular SQLite file")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        try:
            self._connection.execute("PRAGMA query_only = ON")
            self._awb = self._resolve(AWB_TABLE, _AWB_COLUMNS, required=_IDENTITY_FIELDS)
            self._customer = self._resolve(CUSTOMER_TABLE, _CUSTOMER_COLUMNS)
            self._config = self._resolve(CUSTOMER_CONFIG_TABLE, _CONFIG_COLUMNS)
            self.source_sha256 = self._hash_descriptor(descriptor)
        except Exception:
            self._connection.close()
            raise

    def _table_exists(self, table: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is not None

    def _resolve(self, table: str, mapping: dict[str, tuple[str, ...]], *, required: tuple[str, ...] = ()) -> dict[str, str]:
        """Map Core field names onto whichever legacy spelling this snapshot uses."""
        if not self._table_exists(table):
            if required:
                raise ReadOnlySourceContractError(f"awb explorer source lacks {table}")
            return {}
        actual = {str(row[1]).casefold(): str(row[1]) for row in self._connection.execute(f'PRAGMA table_info("{table}")')}
        resolved = {
            field: next((actual[name.casefold()] for name in aliases if name.casefold() in actual), "")
            for field, aliases in mapping.items()
        }
        missing = [field for field in required if not resolved.get(field)]
        if missing:
            raise ReadOnlySourceContractError(f"awb explorer source lacks required {table} columns: {', '.join(missing)}")
        return resolved

    @staticmethod
    def _hash_descriptor(descriptor: int) -> str:
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), "rb") as source:
            source.seek(0)
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return digest.hexdigest()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ---- querying -------------------------------------------------------

    @staticmethod
    def _page(limit: object, offset: object) -> tuple[int, int]:
        try:
            return max(1, min(int(limit), _MAX_PAGE)), max(0, int(offset))
        except (TypeError, ValueError):
            raise ValueError("invalid awb page") from None

    def _select(self, fields: tuple[str, ...]) -> str:
        return ", ".join(
            "a.rowid" if field == "shipment_id" else f'a."{self._awb[field]}"' if self._awb.get(field) else "NULL"
            for field in fields
        )

    def _anomaly_clause(self) -> str:
        """VB6: Total US = 0 OR IS NULL OR EXRATE > 37 OR EXRATE < 30."""
        total = self._awb.get("total_us")
        exrate = self._awb.get("exrate")
        parts = []
        if total:
            parts.append(f'a."{total}" IS NULL OR TRIM(COALESCE(a."{total}", \'\')) = \'\' OR CAST(a."{total}" AS REAL) = 0')
        if exrate:
            low, high = LEGACY_ANOMALY_BAND
            parts.append(f'CAST(a."{exrate}" AS REAL) < {low} OR CAST(a."{exrate}" AS REAL) > {high}')
        return "(" + " OR ".join(parts) + ")" if parts else "1"

    def search_shipments(
        self,
        date_from: object = "",
        date_to: object = "",
        invoice_prefix: object = "",
        awb_prefix: object = "",
        anomaly_only: object = True,
        limit: object = 50,
        offset: object = 0,
    ) -> dict[str, object]:
        page_limit, page_offset = self._page(limit, offset)
        if any(type(value) is not str for value in (date_from, date_to, invoice_prefix, awb_prefix)):
            raise ValueError("invalid awb filter")
        clauses: list[str] = []
        parameters: list[str] = []
        if date_from and self._awb.get("awb_date"):
            clauses.append(f'a."{self._awb["awb_date"]}" >= ?')
            parameters.append(date_from)
        if date_to and self._awb.get("awb_date"):
            clauses.append(f'a."{self._awb["awb_date"]}" <= ?')
            parameters.append(date_to)
        # VB6 uses Like 'x*' — prefix match, not contains, and force-uppercased.
        if invoice_prefix:
            clauses.append(f'UPPER(a."{self._awb["invoice_no"]}") LIKE ? ESCAPE \'\\\'')
            parameters.append(_escape_prefix(invoice_prefix))
        if awb_prefix:
            clauses.append(f'UPPER(a."{self._awb["awb_no"]}") LIKE ? ESCAPE \'\\\'')
            parameters.append(_escape_prefix(awb_prefix))
        if anomaly_only:
            clauses.append(self._anomaly_clause())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        fields = ("shipment_id", "awb_date", "invoice_no", "awb_no", "ship_by", "boxes", "weight", "rate", "total_thb", "total_us", "exrate")
        source = f' FROM "{AWB_TABLE}" AS a'
        with self._lock:
            total = int(self._connection.execute("SELECT COUNT(*)" + source + where, parameters).fetchone()[0])
            rows = self._connection.execute(
                f"SELECT {self._select(fields)}{source}{where} "
                f'ORDER BY a."{self._awb["awb_date"]}", a."{self._awb["invoice_no"]}" LIMIT ? OFFSET ?',
                [*parameters, page_limit, page_offset],
            ).fetchall()
        shipments = []
        for row in rows:
            record = {field: _text(value) for field, value in zip(fields, row)}
            # Unit (net US$/kg) is margin per kilo.  Withheld until Q5 is answered:
            # the column keeps its VB6 position, the value is never computed here.
            record["unit"] = None
            record["unit_withheld"] = True
            record["anomaly_reasons"] = self._anomaly_reasons(record)
            shipments.append(record)
        next_offset = page_offset + page_limit
        return {
            "total": total,
            "limit": page_limit,
            "offset": page_offset,
            "has_more": next_offset < total,
            "next_offset": next_offset if next_offset < total else None,
            "anomaly_only": bool(anomaly_only),
            "anomaly_band": [str(LEGACY_ANOMALY_BAND[0]), str(LEGACY_ANOMALY_BAND[1])],
            "anomaly_band_origin": LEGACY_ANOMALY_ORIGIN,
            "shipments": shipments,
        }

    def _anomaly_reasons(self, record: dict[str, object]) -> list[dict[str, object]]:
        reasons = []
        total_us = _number(record.get("total_us"))
        exrate = _number(record.get("exrate"))
        if total_us is None or total_us == 0:
            reasons.append({"code": "total-us-empty", "text": "Total US$ is empty or zero"})
        if exrate is not None:
            low, high = LEGACY_ANOMALY_BAND
            if exrate < low:
                reasons.append({"code": "exrate-below-band", "text": f"Ex rate {exrate} below {low}"})
            elif exrate > high:
                reasons.append({"code": "exrate-above-band", "text": f"Ex rate {exrate} above {high}"})
        for reason in reasons:
            reason["band"] = f"{LEGACY_ANOMALY_BAND[0]}–{LEGACY_ANOMALY_BAND[1]}"
            reason["origin"] = LEGACY_ANOMALY_ORIGIN
            reason["confirmed"] = False
        return reasons

    def _customer_for(self, invoice_no: str) -> dict[str, object]:
        """VB6 derives the customer as Left(Inv No, InStr('/') - 1) — string-fragile."""
        prefix = invoice_no.split("/", 1)[0] if "/" in invoice_no else ""
        result = {"customer_id": prefix, "customer_name": "", "customer_derived": bool(prefix), "charges": None}
        if not prefix or not self._customer.get("customer_id"):
            return result
        with self._lock:
            row = self._connection.execute(
                f'SELECT "{self._customer["customer_name"] or self._customer["customer_id"]}" '
                f'FROM "{CUSTOMER_TABLE}" WHERE UPPER("{self._customer["customer_id"]}") = UPPER(?) LIMIT 1',
                (prefix,),
            ).fetchone()
            result["customer_name"] = _text(row[0]) if row else ""
            if self._config.get("charges") and self._config.get("customer_id"):
                config = self._connection.execute(
                    f'SELECT "{self._config["charges"]}" FROM "{CUSTOMER_CONFIG_TABLE}" '
                    f'WHERE UPPER("{self._config["customer_id"]}") = UPPER(?) LIMIT 1',
                    (prefix,),
                ).fetchone()
                result["charges"] = _number(config[0]) if config else None
        return result

    def open_shipment(self, invoice_no: object) -> dict[str, object] | None:
        if type(invoice_no) is not str or not invoice_no:
            return None
        return self._open_shipment('a."' + self._awb["invoice_no"] + '" = ?', (invoice_no,))

    def open_shipment_by_id(self, shipment_id: object) -> dict[str, object] | None:
        if type(shipment_id) is not str or not shipment_id.isdecimal() or int(shipment_id) < 1:
            return None
        return self._open_shipment("a.rowid = ?", (int(shipment_id),))

    def _open_shipment(self, predicate: str, parameters: tuple[object, ...]) -> dict[str, object] | None:
        fields = ("invoice_no", "awb_no", "awb_date", "ship_by", "boxes", "destination",
                  "weight", "rate", "agent", "carrier", "total_thb", "total_us", "exrate")
        with self._lock:
            row = self._connection.execute(
                f'SELECT {self._select(fields)} FROM "{AWB_TABLE}" AS a WHERE {predicate} LIMIT 1',
                parameters,
            ).fetchone()
        if row is None:
            return None
        record = {field: _text(value) for field, value in zip(fields, row)}
        customer = self._customer_for(record["invoice_no"])
        missing = [field for field in _IDENTITY_FIELDS if not record.get(field)]
        identity = {
            "invoice_no": record["invoice_no"], "awb_no": record["awb_no"], "awb_date": record["awb_date"],
            "boxes": record["boxes"], "ship_by": record["ship_by"], "destination": record["destination"],
            "customer_id": customer["customer_id"], "customer_name": customer["customer_name"],
            "customer_derived": customer["customer_derived"],
            # No order↔AWB relationship exists anywhere in the VB6 source.
            "order_no": None, "order_no_status": "unmapped",
        }
        payload = {
            "identity": identity,
            "identity_complete": not missing,
            "missing_identifiers": missing,
            "anomaly_reasons": self._anomaly_reasons(record),
            "freight": [],
            "notices": [],
        }
        if missing:
            payload["notices"].append("Freight values withheld — shipment identity incomplete.")
            return payload
        payload["freight"], payload["notices"] = self._ledger(record, customer["charges"])
        return payload

    def _ledger(self, record: dict[str, str], charges: Decimal | None) -> tuple[list[dict[str, object]], list[str]]:
        """A ledger of provenance, not a calculator.  Stored and recomputed stay apart."""
        weight, rate = _number(record["weight"]), _number(record["rate"])
        agent, carrier = _number(record["agent"]), _number(record["carrier"])
        stored_thb, stored_us, exrate = _number(record["total_thb"]), _number(record["total_us"]), _number(record["exrate"])
        notices: list[str] = []

        subtotal = weight * rate if weight is not None and rate is not None else None
        recomputed_thb = None
        if subtotal is not None:
            recomputed_thb = subtotal + (agent or Decimal(0)) + (carrier or Decimal(0))

        recomputed_us = None
        usd_flags: list[str] = []
        if exrate is None or exrate <= 0:
            # VB6 CountTotal skips the USD line entirely and leaves the old value
            # in the box.  A stale number readable as current is the failure mode
            # being designed out; Core says so instead.
            usd_flags.append("exrate-zero")
            notices.append("Grand total USD — not computable (Ex rate is 0 or missing).")
        elif recomputed_thb is None:
            usd_flags.append("thb-not-computable")
        elif charges is None:
            # The VB6 source hard-codes txtCargo = "190.00" with the CUST CON.Charges
            # lookup commented out, and the running build showed 180.00.  Core adopts
            # neither: an unsourced default is how the 10.00 delta happened.
            usd_flags.append("cargo-unknown")
            notices.append("Grand total USD — not computable: no Cargo charge found for this customer. Core does not apply the legacy hard-coded default.")
        else:
            recomputed_us = (recomputed_thb / exrate).quantize(_CENTS, rounding=ROUND_HALF_UP) + charges

        ledger = [
            _line("Weight", "KG", weight, None, f"{AWB_TABLE}.Weight"),
            _line("Rate", "THB", rate, None, f"{AWB_TABLE}.RATE", ["rate-rule-not-verified"]),
            _line("Freight subtotal", "THB", None, subtotal, "Weight × Rate"),
            _line("Agent (1.2)", "THB", agent, None, f"{AWB_TABLE}.Agent"),
            _line("Carrier (1.7)", "THB", carrier, None, f"{AWB_TABLE}.Carrier"),
            _line("Grand total THB", "THB", stored_thb, recomputed_thb, "Subtotal + Agent + Carrier"),
            _line("Ex rate", "", exrate, None, f"{AWB_TABLE}.exRate", ["display-rule-unresolved"]),
            _line("Cargo", "USD", charges, None, f"{CUSTOMER_CONFIG_TABLE}.Charges" if charges is not None else "not found"),
            _line("Grand total USD", "USD", stored_us, recomputed_us, "Total THB ÷ Ex rate + Cargo", usd_flags),
        ]
        return ledger, notices


def _escape_prefix(value: str) -> str:
    escaped = value.upper().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def html() -> str:
    """One literal template.  No str.replace patch chain — the served markup is
    readable directly from this source, unlike the customer explorer's."""
    return """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APC Core &middot; Shipments</title><style>:root{--ink:#24272b;--muted:#68717c;--line:#e5e1db;--cream:#eadbc8;--paper:#fffdfa;--accent:#1d6b57;--list-alt:#f1ede4;--list-hover:#dcefe5;--warn:#8a6100;--warn-bg:#fff8df;--warn-line:#c9a843}*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font:14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.shell{max-width:1500px;margin:auto;padding:28px}.utility{display:flex;flex-wrap:wrap;gap:10px;align-items:center;font-size:12px;color:var(--muted)}.badge{border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:var(--paper);font-weight:700}.badge.ro{color:var(--accent)}.anomaly-chip{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:12px 0;padding:10px 12px;border:1px solid var(--warn-line);border-radius:10px;background:var(--warn-bg);color:var(--warn);line-height:1.4}.anomaly-chip strong{display:block}.pane{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:16px;overflow-x:auto}.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-bottom:12px}label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}input,button{font:inherit;padding:9px;border:1px solid var(--line);border-radius:8px;margin-top:4px}button{cursor:pointer;background:var(--accent);color:#fff;font-weight:700}button.secondary{background:var(--paper);color:var(--accent)}button[disabled]{opacity:.5;cursor:not-allowed}#count{margin-left:auto;font-weight:700}table{width:100%;border-collapse:collapse;min-width:960px}th{position:sticky;top:0;z-index:1;background:var(--paper);font-size:11px;color:var(--muted);text-transform:uppercase;padding:8px 9px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:8px 9px;border-bottom:1px solid #eeeae4;white-space:nowrap}tbody tr:nth-child(even){background:var(--list-alt)}tbody tr{cursor:pointer}tbody tr:hover,tbody tr:focus{background:var(--list-hover);outline:2px solid #b9dbcf;outline-offset:-2px}tbody tr[aria-selected="true"]{background:var(--list-hover);outline:2px solid var(--accent);outline-offset:-2px}.num{text-align:right}.mid{text-align:center}.ship-by{min-width:130px}.flag{color:var(--warn);font-weight:700}.withheld{color:var(--muted)}.empty{padding:26px;text-align:center;color:var(--muted)}.rail{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:14px}.guarded{display:inline-flex;align-items:center;gap:6px;border:1px dashed var(--warn-line);border-radius:8px;padding:8px 10px;background:var(--warn-bg);color:var(--warn);font-size:12px}.guarded b{font-weight:700}.modal{position:fixed;inset:0;background:#0007;display:grid;place-items:center;padding:18px;z-index:12}.modal[hidden]{display:none}.dialog{width:min(1040px,100%);max-height:92vh;overflow:auto;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px}.dialog-head{display:flex;gap:10px;align-items:center;margin-bottom:12px}.dialog-head h2{margin:0;font-size:19px}.dialog-head button{margin-left:auto}.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:16px}.fields{display:grid;gap:8px;margin:0}.fields div{border:1px solid var(--line);border-radius:8px;padding:8px}.fields dt{font-size:11px;font-weight:700;color:var(--muted)}.fields dd{margin:3px 0 0;overflow-wrap:anywhere}.ledger{width:100%;min-width:0;border-collapse:collapse}.ledger th{position:static}.ledger td{white-space:normal}.agree{color:var(--accent);font-weight:700}.notice{margin:10px 0;padding:9px 11px;border-left:3px solid var(--warn-line);background:var(--warn-bg);color:var(--warn);font-size:12px;line-height:1.4}@media(max-width:900px){.shell{padding:14px}.two{grid-template-columns:1fr}.filters input{width:100%}}</style><body><main class="shell"><a class="back" href="../">Main menu</a><h1>Shipments</h1><div class="utility"><span class="badge ro">READ-ONLY</span><span class="badge" id="snapshot">snapshot &mdash;</span><span>Core &rsaquo; Shipments &rsaquo; AWB</span></div><section class="anomaly-chip" id="anomaly-chip" aria-live="polite"><div><strong>Legacy anomaly view &mdash; ON</strong><span id="anomaly-text">Showing only records with an empty Total US$ or an Ex rate outside the legacy band.</span></div><button id="anomaly-toggle" type="button" class="secondary">Show all records</button></section><section class="pane"><div class="filters"><label>AWB date from<input id="date-from" type="date"></label><label>AWB date to<input id="date-to" type="date"></label><label>Invoice no.<input id="invoice" maxlength="15" autocomplete="off" placeholder="Prefix"></label><label>AWB no.<input id="awb" maxlength="20" autocomplete="off" placeholder="Prefix"></label><button id="search" type="button">Search</button><strong id="count">Total Record(s): 0</strong></div><table><thead><tr><th>AWB Date</th><th class="num">Invoice No.</th><th class="mid">AWB No.</th><th class="ship-by">Ship By</th><th class="mid">Box</th><th class="num">Weight</th><th class="num">Rate/KG</th><th class="num">Total THB</th><th class="num">Total US$ (stored)</th><th class="num">Unit</th></tr></thead><tbody id="rows"></tbody></table><div id="empty" class="empty" hidden>No shipments match these filters.</div><button id="more" type="button" class="secondary" hidden>Load more</button><div class="rail"><button id="open" type="button" disabled>Open shipment</button><span class="guarded">Save &mdash; <b>writes the AWB row and an airfreight accounting entry</b></span><span class="guarded">Delete &mdash; <b>cancels the accounting entry and deletes the AWB row</b></span><span class="guarded">Print &mdash; <b>stages rows into TempDB and runs a Crystal report</b></span><span class="guarded">Print Preview &mdash; <b>guarded with Print</b></span></div></section></main><section id="shipment-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="shipment-title" hidden><div class="dialog"><div class="dialog-head"><h2 id="shipment-title">Open shipment</h2><button id="close" type="button" class="secondary">Close</button></div><div id="modal-notices"></div><div class="two"><section><h3>Identity</h3><dl class="fields" id="identity"></dl></section><section><h3>Freight &mdash; stored vs recomputed</h3><table class="ledger"><thead><tr><th>Line</th><th class="num">Stored</th><th class="num">Recomputed</th><th>Source</th></tr></thead><tbody id="ledger"></tbody></table><div id="why-flagged"></div></section></div><div class="rail"><span class="guarded">OK / Save, Delete and Print are <b>guarded &mdash; this screen never writes</b></span></div></div></section><script>(()=>{const $=s=>document.querySelector(s),modal=$('#shipment-modal'),rows=$('#rows');let selected='',anomalyOnly=true,lastRow=null,loaded=0;
const iso=days=>{const d=new Date();d.setDate(d.getDate()+days);return d.toISOString().slice(0,10)};
$('#date-from').value=iso(-365);$('#date-to').value=iso(3);
const cell=(value,cls)=>{const td=document.createElement('td');if(cls)td.className=cls;td.textContent=value===null||value===undefined?'':value;return td};
function getJSON(path){return fetch(path,{credentials:'same-origin',cache:'no-store'}).then(r=>r.ok?r.json():Promise.reject(new Error('Read failed')))}
function params(offset){const p=new URLSearchParams({limit:100,offset,anomaly_only:anomalyOnly?'1':'0'});p.set('date_from',$('#date-from').value);p.set('date_to',$('#date-to').value);p.set('invoice',$('#invoice').value);p.set('awb',$('#awb').value);return p}
function select(tr,shipment){if(lastRow)lastRow.setAttribute('aria-selected','false');lastRow=tr;tr.setAttribute('aria-selected','true');selected=shipment.shipment_id;$('#open').disabled=!selected}
function rowFor(shipment){const tr=document.createElement('tr');tr.tabIndex=0;tr.setAttribute('aria-selected','false');tr.append(cell(shipment.awb_date),cell(shipment.invoice_no,'num'),cell(shipment.awb_no,'mid'),cell(shipment.ship_by,'ship-by'),cell(shipment.boxes,'mid'),cell(shipment.weight,'num'),cell(shipment.rate,'num'),cell(shipment.total_thb,'num'),cell(shipment.total_us,'num'));const unit=cell('\\u2014','num withheld');unit.title='Unit (net US$/kg) is a margin figure. Withheld pending a disclosure decision.';tr.append(unit);if(shipment.anomaly_reasons.length){const mark=document.createElement('span');mark.className='flag';mark.textContent=' \\u2691';mark.title=shipment.anomaly_reasons.map(r=>r.text).join('; ');tr.lastChild.append(mark)}
tr.onclick=()=>select(tr,shipment);tr.ondblclick=()=>{select(tr,shipment);open(shipment.shipment_id)};tr.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();select(tr,shipment);open(shipment.shipment_id)}else if(event.key==='ArrowDown'&&tr.nextElementSibling){event.preventDefault();tr.nextElementSibling.focus()}else if(event.key==='ArrowUp'&&tr.previousElementSibling){event.preventDefault();tr.previousElementSibling.focus()}};return tr}
function load(append){const offset=append?loaded:0;return getJSON('api/shipments?'+params(offset)).then(data=>{if(!append){rows.replaceChildren();loaded=0;selected='';lastRow=null;$('#open').disabled=true}
data.shipments.forEach(shipment=>rows.append(rowFor(shipment)));loaded+=data.shipments.length;$('#count').textContent='Total Record(s): '+data.total;$('#empty').hidden=Boolean(data.total);$('#more').hidden=!data.has_more;$('#anomaly-text').textContent=anomalyOnly?'Showing only records with an empty Total US$ or an Ex rate outside '+data.anomaly_band.join('\\u2013')+' (legacy rule, pending confirmation).':'Showing all records. The legacy default hides some of these.';$('#anomaly-chip').querySelector('strong').textContent=anomalyOnly?'Legacy anomaly view \\u2014 ON':'Legacy anomaly view \\u2014 OFF';$('#anomaly-toggle').textContent=anomalyOnly?'Show all records':'Return to legacy anomaly view';
if(!append&&rows.firstElementChild)rows.firstElementChild.focus({preventScroll:true})}).catch(()=>{$('#empty').hidden=false;$('#empty').textContent='Shipments could not be loaded.'})}
function field(term,value,note){const box=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=term;dd.textContent=value||'\\u2014';if(note){const small=document.createElement('span');small.className='withheld';small.textContent=' '+note;dd.append(small)}box.append(dt,dd);return box}
function ledgerRow(line){const tr=document.createElement('tr'),label=cell(line.label+(line.unit?' ('+line.unit+')':''));tr.append(label,cell(line.stored,'num'),cell(line.recomputed,'num'));const source=cell(line.source);if(line.agrees===true){const tick=document.createElement('span');tick.className='agree';tick.textContent='\\u2713 agree \\u00b7 ';source.prepend(tick)}else if(line.agrees===false){const delta=document.createElement('span');delta.className='flag';delta.textContent='\\u0394 '+line.delta+' \\u00b7 ';source.prepend(delta)}
if(line.flags.includes('exrate-zero')||line.flags.includes('cargo-unknown')||line.flags.includes('thb-not-computable')){tr.children[2].textContent='not computable';tr.children[2].className='num flag'}
if(line.flags.includes('rate-rule-not-verified'))source.append(' \\u2014 rule not verified');tr.append(source);return tr}
function open(invoice){getJSON('api/shipments/'+encodeURIComponent(invoice)).then(data=>{const id=data.identity;$('#shipment-title').textContent='Shipment '+(id.invoice_no||'\\u2014');
$('#identity').replaceChildren(field('Invoice No.',id.invoice_no),field('AWB No.',id.awb_no),field('AWB Date',id.awb_date),field('Boxes',id.boxes),field('Shipped By',id.ship_by),field('Destination',id.destination),field('Customer',[id.customer_id,id.customer_name].filter(Boolean).join(' \\u00b7 '),id.customer_derived?'(derived from invoice prefix)':''),field('Order No.','unmapped','\\u2014 no order\\u2194AWB link exists in the legacy source'));
const notices=$('#modal-notices');notices.replaceChildren();if(!data.identity_complete)data.notices.unshift('Missing: '+data.missing_identifiers.join(', '));data.notices.forEach(text=>{const p=document.createElement('p');p.className='notice';p.textContent=text;notices.append(p)});
$('#ledger').replaceChildren(...data.freight.map(ledgerRow));
const why=$('#why-flagged');why.replaceChildren();if(data.anomaly_reasons.length){const p=document.createElement('p');p.className='notice';p.textContent='Why flagged: '+data.anomaly_reasons.map(r=>r.text).join('; ')+' \\u2014 band '+data.anomaly_reasons[0].band+', from '+data.anomaly_reasons[0].origin;why.append(p)}
modal.hidden=false;$('#close').focus({preventScroll:true})}).catch(()=>{})}
function close(){modal.hidden=true;if(lastRow)lastRow.focus({preventScroll:true})}
$('#close').onclick=close;modal.onclick=event=>{if(event.target===modal)close()};
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!modal.hidden){event.preventDefault();close()}});
$('#open').onclick=()=>{if(selected)open(selected)};
$('#search').onclick=()=>load(false);
$('#more').onclick=()=>load(true);
$('#anomaly-toggle').onclick=()=>{anomalyOnly=!anomalyOnly;load(false)};
['#invoice','#awb'].forEach(id=>{const node=$(id);node.oninput=()=>node.value=node.value.toUpperCase();node.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();load(false)}}});
['#date-from','#date-to'].forEach(id=>$(id).onchange=()=>load(false));
load(false);})();</script></body></html>"""

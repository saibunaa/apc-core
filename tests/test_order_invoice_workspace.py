from dataclasses import FrozenInstanceError
from pathlib import Path
import sqlite3
import tempfile
import unittest


class TestOrderInvoiceWorkspaceContract(unittest.TestCase):
    module_path = Path(__file__).parents[1] / "apc_core" / "order_invoice_workspace.py"

    def test_00_workspace_mapping_module_is_required(self):
        assert self.module_path.is_file(), "apc_core/order_invoice_workspace.py must exist"

    def test_source_order_is_a_read_only_namespaced_source_render_dto(self):
        from apc_core.order_invoice_workspace import SourceRenderDTO, map_source_order

        dto = map_source_order(
            {
                "order_id": "ORD/2026/001",
                "customer_id": "C/001",
                "customer_name": "Customer One",
                "lines": [{"line_no": "2", "item_id": "ITEM-2", "qty": "5"}],
            },
            source_sha256="a" * 64,
        )

        assert type(dto) is SourceRenderDTO
        assert dto.record_type == "source_order"
        assert dto.record_id == "source_order:ORD/2026/001"
        assert dto.source_sha256 == "a" * 64
        assert dto.customer_id == "C/001"
        assert dto.customer_name == "Customer One"
        assert dto.line_page == ((("line_no", "2"), ("item_id", "ITEM-2"), ("qty", "5")),)
        assert dto.line_total == 1
        assert dto.next_offset is None
        assert dto.read_only is True
        assert not hasattr(dto, "mutation_controls")
        try:
            dto.customer_name = "changed"
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("source render DTO must be immutable")

    def test_source_invoice_uses_its_own_namespace_and_never_carries_order_provenance(self):
        from apc_core.order_invoice_workspace import SourceRenderDTO, map_source_invoice

        dto = map_source_invoice(
            {
                "invoice_id": "INV//2026/001",
                "header": {"customer_id": "C/002", "customer_name": "Customer Two"},
                "lines": [{"line_no": "1", "item_id": "ITEM-1", "amount": "25"}],
                "total": 3,
                "next_offset": 1,
            },
            source_sha256="b" * 64,
        )

        assert type(dto) is SourceRenderDTO
        assert dto.record_type == "source_invoice"
        assert dto.record_id == "source_invoice:INV//2026/001"
        assert dto.source_sha256 == "b" * 64
        assert dto.customer_id == "C/002"
        assert dto.customer_name == "Customer Two"
        assert dto.line_page == ((("line_no", "1"), ("item_id", "ITEM-1"), ("amount", "25")),)
        assert dto.line_total == 3
        assert dto.next_offset == 1
        assert dto.read_only is True
        assert "order" not in repr(dto).casefold()
        assert not hasattr(dto, "mutation_controls")

    def test_core_draft_is_distinct_non_source_read_only_render_dto(self):
        from apc_core.order_invoice_workspace import CoreDraftRenderDTO, map_core_draft

        dto = map_core_draft(
            {
                "draft_id": "draft-7",
                "customer_id": "C/003",
                "customer_name": "Customer Three",
                "lines": [{"line_no": 1, "order_id": "ORD/3", "item_id": "ITEM-3", "quantity": "2"}],
            }
        )

        assert type(dto) is CoreDraftRenderDTO
        assert dto.record_type == "core_draft"
        assert dto.record_id == "core_draft:draft-7"
        assert not hasattr(dto, "source_sha256")
        assert dto.customer_id == "C/003"
        assert dto.customer_name == "Customer Three"
        assert dto.line_page == ((("line_no", 1), ("order_id", "ORD/3"), ("item_id", "ITEM-3"), ("quantity", "2")),)
        assert dto.line_total == 1
        assert dto.next_offset is None
        assert dto.read_only is True
        assert "source" not in repr(dto).casefold()
        assert not hasattr(dto, "mutation_controls")

    def test_source_invoice_rejects_all_order_provenance_keys_in_headers_and_lines(self):
        from apc_core.order_invoice_workspace import map_source_invoice

        for location, key in (
            ("header", "order_id"),
            ("header", "order_no"),
            ("header", "order_number"),
            ("header", "order_reference"),
            ("line", "order_id"),
            ("line", "order_no"),
            ("line", "order_number"),
            ("line", "order_reference"),
        ):
            with self.subTest(location=location, key=key):
                invoice = {
                    "invoice_id": "INV/1",
                    "header": {"customer_id": "C", "customer_name": "Name"},
                    "lines": [{"line_no": "1", "item_id": "ITEM"}],
                    "total": 1,
                    "next_offset": None,
                }
                if location == "header":
                    invoice["header"][key] = "ORD/1"
                else:
                    invoice["lines"][0][key] = "ORD/1"
                with self.assertRaises(ValueError):
                    map_source_invoice(invoice, source_sha256="a" * 64)

    def test_core_draft_rejects_source_sha256_at_every_nested_level(self):
        from apc_core.order_invoice_workspace import map_core_draft

        for location in ("top", "header", "line"):
            with self.subTest(location=location):
                draft = {
                    "draft_id": "draft-1",
                    "customer_id": "C",
                    "customer_name": "Name",
                    "header": {},
                    "lines": [{"line_no": "1", "item_id": "ITEM"}],
                }
                if location == "top":
                    draft["source_sha256"] = "a" * 64
                elif location == "header":
                    draft["header"]["source_sha256"] = "a" * 64
                else:
                    draft["lines"][0]["source_sha256"] = "a" * 64
                with self.assertRaises(ValueError):
                    map_core_draft(draft)

    def test_source_data_rejects_mutation_controls_at_every_nested_level(self):
        from apc_core.order_invoice_workspace import map_source_invoice, map_source_order

        for mapper, payload in (
            (map_source_order, {"order_id": "ORD/1", "customer_id": "C", "customer_name": "Name", "header": {}, "lines": [{"line_no": "1", "item_id": "ITEM"}]}),
            (map_source_invoice, {"invoice_id": "INV/1", "header": {"customer_id": "C", "customer_name": "Name"}, "lines": [{"line_no": "1", "item_id": "ITEM"}], "total": 1, "next_offset": None}),
        ):
            for location, key in (("top", "mutation_controls"), ("header", "mutation_control"), ("line", "actions")):
                with self.subTest(mapper=mapper.__name__, location=location, key=key):
                    candidate = {**payload, "header": dict(payload.get("header", {})), "lines": [dict(payload["lines"][0])]}
                    if location == "top":
                        candidate[key] = "delete"
                    elif location == "header":
                        candidate["header"][key] = "update"
                    else:
                        candidate["lines"][0][key] = "create"
                    with self.assertRaises(ValueError):
                        mapper(candidate, source_sha256="a" * 64)

    def test_recursive_boundary_guards_reject_tuple_nested_forbidden_keys(self):
        from apc_core.order_invoice_workspace import map_core_draft, map_source_invoice, map_source_order
        with self.assertRaises(ValueError):
            map_source_invoice({"invoice_id":"INV/1","header":{"customer_id":"C","customer_name":"N","nested":({"order_reference":"ORD/1"},)},"lines":[],"total":0,"next_offset":None}, source_sha256="a" * 64)
        with self.assertRaises(ValueError):
            map_source_order({"order_id":"ORD/1","customer_id":"C","customer_name":"N","header":({"mutation_control":"write"},),"lines":[]}, source_sha256="a" * 64)
        with self.assertRaises(ValueError):
            map_core_draft({"draft_id":"D","customer_id":"C","customer_name":"N","header":({"source_sha256":"a" * 64},),"lines":[]})

    def test_recursive_boundary_guards_reject_nested_dict_subclasses(self):
        from apc_core.order_invoice_workspace import map_core_draft, map_source_invoice, map_source_order

        class SneakyDict(dict):
            pass

        with self.assertRaises(ValueError):
            map_source_invoice(
                {
                    "invoice_id": "INV/1",
                    "header": {"customer_id": "C", "customer_name": "N"},
                    "metadata": SneakyDict({"order_reference": "ORD/1"}),
                    "lines": [],
                    "total": 0,
                    "next_offset": None,
                },
                source_sha256="a" * 64,
            )
        with self.assertRaises(ValueError):
            map_source_order(
                {
                    "order_id": "ORD/1",
                    "customer_id": "C",
                    "customer_name": "N",
                    "metadata": SneakyDict({"mutation_control": "write"}),
                    "lines": [],
                },
                source_sha256="a" * 64,
            )
        with self.assertRaises(ValueError):
            map_core_draft(
                {
                    "draft_id": "D",
                    "customer_id": "C",
                    "customer_name": "N",
                    "metadata": SneakyDict({"source_sha256": "a" * 64}),
                    "lines": [],
                }
            )

    def test_recursive_boundary_guards_reject_container_subclasses_that_hide_contents(self):
        from apc_core.order_invoice_workspace import map_core_draft, map_source_invoice, map_source_order

        class HidingDict(dict):
            def items(self):
                return {}.items()

        class HidingList(list):
            def __iter__(self):
                return iter(())

        class HidingTuple(tuple):
            def __iter__(self):
                return iter(())

        with self.assertRaises(ValueError):
            map_source_invoice(
                {
                    "invoice_id": "INV/1",
                    "header": {"customer_id": "C", "customer_name": "N"},
                    "metadata": HidingDict({"order_reference": "ORD/1"}),
                    "lines": [],
                    "total": 0,
                    "next_offset": None,
                },
                source_sha256="a" * 64,
            )
        with self.assertRaises(ValueError):
            map_source_order(
                {
                    "order_id": "ORD/1",
                    "customer_id": "C",
                    "customer_name": "N",
                    "metadata": HidingList([{"mutation_control": "write"}]),
                    "lines": [],
                },
                source_sha256="a" * 64,
            )
        with self.assertRaises(ValueError):
            map_core_draft(
                {
                    "draft_id": "D",
                    "customer_id": "C",
                    "customer_name": "N",
                    "metadata": HidingTuple(({"source_sha256": "a" * 64},)),
                    "lines": [],
                }
            )

    def test_mapping_helpers_reject_cross_family_shapes_and_keep_line_pages_immutable(self):
        from apc_core.order_invoice_workspace import map_core_draft, map_source_invoice, map_source_order

        try:
            map_source_order({"invoice_id": "INV", "lines": []}, source_sha256="a" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("source order mapper must reject an invoice shape")

        try:
            map_source_invoice({"order_id": "ORD", "lines": []}, source_sha256="a" * 64)
        except ValueError:
            pass
        else:
            raise AssertionError("source invoice mapper must reject an order shape")

        try:
            map_core_draft({"draft_id": "draft", "lines": [] , "source_sha256": "a" * 64})
        except ValueError:
            pass
        else:
            raise AssertionError("core draft mapper must reject source provenance")

        dto = map_source_order(
            {"order_id": "ORD", "customer_id": "C", "customer_name": "Name", "lines": [{"item_id": "I"}]},
            source_sha256="c" * 64,
        )
        try:
            dto.line_page[0] += (("item_id", "changed"),)
        except (AttributeError, TypeError):
            pass
        else:
            raise AssertionError("render line page must be immutable")

    def test_source_order_mapping_retains_bounded_page_metadata(self):
        from apc_core.order_invoice_workspace import map_source_order

        dto = map_source_order(
            {
                "order_id": "ORD/2026/LARGE",
                "customer_id": "C/001",
                "customer_name": "Customer One",
                "lines": [{"line_no": "1", "item_id": "ITEM-1", "qty": "1"}],
                "total": 1661,
                "next_offset": 1,
            },
            source_sha256="d" * 64,
        )

        self.assertEqual(1661, dto.line_total)
        self.assertEqual(1, dto.next_offset)

    def test_source_order_reader_pages_all_historical_capacity_fixtures_without_full_first_page(self):
        from apc_core.order_explorer import OrderExplorer
        from tests.test_order_explorer import TestOrderExplorerContract

        for line_count in (86, 250, 1076, 1363, 1661):
            with self.subTest(line_count=line_count), tempfile.TemporaryDirectory() as tmp:
                fixture = TestOrderExplorerContract()
                source = fixture.make_snapshot(Path(tmp))
                connection = sqlite3.connect(source)
                connection.executemany(
                    'INSERT INTO "MainDB__ORDER_ITEM" VALUES (?, ?, ?, ?)',
                    [("ORD/2026/LARGE", str(index), f"ITEM-{index}", "1") for index in range(line_count)],
                )
                connection.execute(
                    'INSERT INTO "MainDB__ORDER" VALUES (?, ?, ?)',
                    ("ORD/2026/LARGE", "2026-08-30", "C/001"),
                )
                connection.commit()
                connection.close()
                explorer = OrderExplorer(source)
                first = explorer.open_order("ORD/2026/LARGE", limit=250, offset=0)
                self.assertEqual(line_count, first["total"])
                self.assertEqual(min(250, line_count), len(first["lines"]))
                self.assertEqual(250 if line_count > 250 else None, first["next_offset"])
                self.assertEqual([str(index) for index in range(min(250, line_count))], [line["line_no"] for line in first["lines"]])
                if line_count > 250:
                    last_offset = ((line_count - 1) // 250) * 250
                    last = explorer.open_order("ORD/2026/LARGE", limit=250, offset=last_offset)
                    self.assertEqual(line_count - last_offset, len(last["lines"]))
                    self.assertIsNone(last["next_offset"])
                explorer.close()

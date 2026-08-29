import unittest
from pathlib import Path


class TestInvoiceDraftPreviews(unittest.TestCase):
    def test_registry_issues_opaque_one_use_expiring_server_held_preview(self):
        from apc_core.invoice_draft_previews import InvoiceDraftPreviewRegistry
        now = [100]
        registry = InvoiceDraftPreviewRegistry(max_pending=1, ttl_seconds=10, clock=lambda: now[0], token_factory=lambda: "opaque-token-1234")
        proposal = {"ready_to_save": True, "selected_order_ids": ("ORD-1",)}
        preview_ref = registry.issue(proposal, "a" * 64)
        self.assertEqual("opaque-token-1234", preview_ref)
        self.assertEqual((proposal, "a" * 64), registry.consume(preview_ref))
        self.assertIsNone(registry.consume(preview_ref))
        registry.issue(proposal, "b" * 64)
        now[0] = 111
        self.assertIsNone(registry.consume("opaque-token"))

    def test_registry_module_exists_before_behavior_contracts(self):
        self.assertTrue((Path(__file__).parents[1] / "apc_core" / "invoice_draft_previews.py").is_file())


if __name__ == "__main__":
    unittest.main()

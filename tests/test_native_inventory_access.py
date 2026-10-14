"""No production inventories needed for bounded native access/capture checks."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import tempfile
import unittest

from native_inventory_access import (
    capture_updated_stores, checkpoint_token, iter_activity_metadata,
    legacy_delta_base_columnar_scope, open_inventory_store,
)


def checkpoint(directory, *, base=None):
    directory.mkdir()
    payload = directory / "payload.bin"
    payload.write_bytes(b"inventory fixture")
    manifest = {"generation": 0}
    if base:
        manifest["base_checkpoint"] = base.name
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "checksums.json").write_text(json.dumps({"payload.bin": hashlib.sha256(payload.read_bytes()).hexdigest()}), encoding="utf-8")
    return directory


class Store:
    def __init__(self):
        self.generation = 0
        self._state = SimpleNamespace(activities={3: {"name": "background"}, 5: {"name": "market"}},
                                      activity_order=[3, 5])
    def activity(self, activity_id):
        raise AssertionError("Metadata scans must not materialize exchanges")
    def _iter_storage_activities(self):
        raise AssertionError("Metadata scans must not allocate exchange-ID tuples")


class NativeAccessTests(unittest.TestCase):
    def test_direct_metadata_without_activity_or_exchange_id_reads(self):
        store = Store()
        rows = list(iter_activity_metadata(SimpleNamespace(_store=store)))
        self.assertEqual([activity_id for activity_id, _ in rows], [3, 5])
        self.assertIs(rows[0][1], store._state.activities[3])

    def test_public_reporting_reader_preferred_with_old_api_fallback(self):
        observed = []
        class InventoryStore:
            @classmethod
            def open(cls, path):
                observed.append("normal")
                return "normal"

            @classmethod
            def open_for_reporting(cls, path):
                observed.append("reporting")
                return "reporting"

        api = SimpleNamespace(InventoryStore=InventoryStore)
        scenario = {"_inventory_checkpoint": "fixture", "_inventory_export_handoff": "stale"}
        self.assertEqual(open_inventory_store(scenario, api), "reporting")
        del InventoryStore.open_for_reporting
        self.assertEqual(open_inventory_store(scenario, api), "normal")
        self.assertEqual(observed, ["reporting", "normal"])
        scenario["_inventory_store"] = "live"
        self.assertEqual(open_inventory_store(scenario, api), "live")

    def test_delta_base_is_opened_columnarly_and_open_is_restored(self):
        calls = []

        class InventoryStore:
            @classmethod
            def open(cls, path):
                calls.append(("normal", path))
                if path == "scenario-delta":
                    return cls.open("legacy-base")
                return "unexpected legacy base"

        def open_checkpoint(path, *, prefer_columnar=False):
            calls.append(("columnar", path, prefer_columnar))
            return "columnar base"

        api = SimpleNamespace(
            InventoryStore=InventoryStore,
            _open_checkpoint=open_checkpoint,
            mark_checkpoint=lambda path: calls.append(("marked", path)),
        )
        original = InventoryStore.__dict__["open"]

        with legacy_delta_base_columnar_scope(api):
            self.assertEqual(InventoryStore.open("scenario-delta"), "columnar base")

        self.assertIs(InventoryStore.__dict__["open"], original)
        self.assertEqual(
            calls,
            [
                ("normal", "scenario-delta"),
                ("columnar", Path("legacy-base"), True),
                ("marked", Path("legacy-base")),
            ],
        )

    def test_receipt_detects_payload_base_and_handoff_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = checkpoint(Path(directory) / "base")
            delta = checkpoint(Path(directory) / "delta", base=base)
            scenario = {"_inventory_checkpoint": delta}
            before = checkpoint_token(scenario)
            (base / "payload.bin").write_bytes(b"changed source")
            self.assertNotEqual(checkpoint_token(scenario), before)
            before = checkpoint_token(scenario)
            (delta / "manifest.json").write_text('{"generation":1,"base_checkpoint":"base"}', encoding="utf-8")
            self.assertNotEqual(checkpoint_token(scenario), before)
            live = Store()
            scenario["_inventory_store"] = live
            before = checkpoint_token(scenario)
            live.generation += 1
            self.assertNotEqual(checkpoint_token(scenario), before)
        store = Store()
        scenario = {"_inventory_export_handoff": store}
        before = checkpoint_token(scenario)
        store.generation = 1
        self.assertNotEqual(before, checkpoint_token(scenario))
        self.assertEqual(checkpoint_token({}), ("missing",))

    def test_nested_callbacks_bound_store_order_and_report_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = checkpoint(Path(directory) / "checkpoint")
            store, calls, seen = Store(), [], []
            report = SimpleNamespace(payload="immutable report")
            registry = {"case": report}
            builder = SimpleNamespace(_validation_reports=registry)
            def publish(scenario_definition, runtime_scenario, *, persist):
                calls.append("publish")
                scenario_definition["_inventory_checkpoint"] = path
                return store
            builder._store_updated_scenario = publish
            api = SimpleNamespace(InventoryStore=SimpleNamespace(open=lambda path: self.fail("Fresh stores must not reopen")))
            def callback(label):
                def capture(scene, returned):
                    calls.append(label)
                    seen.append(open_inventory_store(scene, api))
                    self.assertIs(returned, store)
                    builder._validation_reports["case"] = SimpleNamespace(payload="staged report")
                return capture
            with capture_updated_stores(builder, callback("por"), priority=20):
                with capture_updated_stores(builder, callback("metals"), priority=10):
                    builder._store_updated_scenario({}, {}, persist=True)
            self.assertEqual(calls, ["publish", "metals", "por"])
            self.assertEqual(seen, [store, store])
            self.assertIs(builder._validation_reports, registry)
            self.assertIs(registry["case"], report)
            self.assertIs(builder._store_updated_scenario, publish)
            self.assertFalse(hasattr(builder, "_repo_native_store_capture"))

    def test_changed_generation_does_not_reuse_bound_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = checkpoint(Path(directory) / "checkpoint")
            store, replacement = Store(), Store()
            builder = SimpleNamespace(_store_updated_scenario=lambda scenario_definition: store)
            class InventoryStore:
                @classmethod
                def open(cls, path):
                    return replacement

            api = SimpleNamespace(InventoryStore=InventoryStore)
            def callback(scene, returned):
                returned.generation += 1
                self.assertIs(open_inventory_store(scene, api), replacement)
            with capture_updated_stores(builder, callback):
                builder._store_updated_scenario({"_inventory_checkpoint": path})

    def test_callback_error_restores_hook_and_registry(self):
        store, previous = Store(), object()
        builder = SimpleNamespace(_validation_reports={"case": previous})
        original_registry = builder._validation_reports
        publisher = lambda scenario_definition: store
        builder._store_updated_scenario = publisher
        def fail(scene, returned):
            builder._validation_reports = {"new": object()}
            raise RuntimeError("staging failed")
        with self.assertRaisesRegex(RuntimeError, "staging failed"):
            with capture_updated_stores(builder, fail):
                builder._store_updated_scenario({"_inventory_store": store})
        self.assertIs(builder._store_updated_scenario, publisher)
        self.assertIs(builder._validation_reports, original_registry)
        self.assertIs(builder._validation_reports["case"], previous)

    def test_unknown_publisher_contract_is_not_intercepted(self):
        publisher = lambda unexpected_name: None
        builder = SimpleNamespace(_store_updated_scenario=publisher)
        with capture_updated_stores(builder, lambda *args: self.fail("Unsupported publisher")) as enabled:
            self.assertFalse(enabled)
        self.assertIs(builder._store_updated_scenario, publisher)


class ActualNativeReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_metals_policy_workflow import NativeWorkflowIntegrationTest
        cls.fixture = NativeWorkflowIntegrationTest
        cls.fixture.setUpClass()
        cls.api = cls.fixture.api

    @classmethod
    def tearDownClass(cls):
        cls.fixture.tearDownClass()

    def test_legacy_arrow_lazy_reader_and_writable_fork_are_equivalent(self):
        from tests.test_metals_policy_workflow import fixture_database
        database = fixture_database()
        database[0]["exchanges"][1].update({"minimum": None, "scale": None, "comment": "retained"})
        before = deepcopy(database)
        with tempfile.TemporaryDirectory() as directory:
            original = self.api.create_inventory_store(database, backend="legacy", scenario_identity="fixture")
            path = original.checkpoint(Path(directory) / "legacy.inventory-store")
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["backend"], "legacy")
            normal = self.api.InventoryStore.open(path)
            reader = open_inventory_store({"_inventory_checkpoint": path}, self.api)
            self.assertEqual(reader.materialize(restore_metadata=True), normal.materialize(restore_metadata=True))
            self.assertEqual(reader.generation, normal.generation)
            writable = reader.fork()
            with writable.transaction("fixture") as transaction:
                exchange_id = writable._state.activity_exchanges[0][1]
                transaction.patch_exchange(exchange_id, {"amount": .7}, activity_id=0)
            self.assertEqual(reader.activity(0).to_dict()["exchanges"][1]["amount"], .8)
            self.assertEqual(writable.activity(0).to_dict()["exchanges"][1]["amount"], .7)
        self.assertEqual(database, before)


if __name__ == "__main__":
    unittest.main()

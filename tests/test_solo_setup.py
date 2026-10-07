"""Solo mode validation, dialog contracts and disposable PostgreSQL acceptance."""
import copy
import json
import os
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
from uuid import uuid4
from zoneinfo import ZoneInfo

from admin_auth import create_or_update_admin
from admin_http import server
from admin_service import (AdminConflict, create_manual_booking, create_service, save_resource,
                           snapshot, update_service, update_weekly_schedule, update_weekly_schedule_batch)
from booking_core import connect, get_available_slots, migrate, stamp
from constructor_store import ConstructorError, constructor_snapshot, delete_parameter, save_entity, save_parameter
from pg_store import SalonScope
from salon_service import attach_master, grant_membership
from shared_schema import provision_salon
from solo_setup import get_setup, save_setup, save_schedule
from vk_gateway import CallbackGateway

UTC = timezone.utc
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath("starter_data.json").read_text())["policy"]
RAW_URL = os.environ.get("SOLO_TEST_DATABASE_URL", "")
PASSWORD = "synthetic-solo-test-7463"


def days(start=540, end=1080):
    return [{"weekday": day, "intervals": [{"start_minute": start, "end_minute": end}]}
            for day in range(7)]


class SoloValidation(unittest.TestCase):
    def test_mode_and_payload_validation_precedes_database_access(self):
        invalid = [None, [], {}, {"mode": "unknown"}, {"mode": True},
                   {"mode": "solo"}, {"mode": "solo", "master_name": " "},
                   {"mode": "team", "reconcile_services": 1},
                   {"mode": "team", "hours_source": "automatic"},
                   {"mode": "team", "expected_master_id": None},
                   {"mode": "team", "expected_master_id": True, "expected_room_id": 2}]
        with patch("solo_setup.connect") as db:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    save_setup("unused", POLICY, payload, "admin:test")
            db.assert_not_called()

    def test_schedule_rejects_partial_or_coerced_expected_ids_before_access(self):
        with patch("solo_setup.connect") as db:
            for expectations in ({"expected_room_id": None},
                                 {"expected_master_id": 1, "expected_room_id": "2"},
                                 {"expected_master_id": 0, "expected_room_id": 2}):
                with self.subTest(expectations=expectations), self.assertRaises(ValueError):
                    save_schedule("unused", POLICY, days(), "admin:test", expectations=expectations)
            db.assert_not_called()

    def test_shared_schedule_requires_seven_distinct_valid_days(self):
        invalid = [[], days()[:6], days()+days()[:1], days()[:6]+days()[:1],
                   [{"weekday": True, "intervals": []}]+days()[1:],
                   [{"weekday": 0, "intervals": [{"start_minute": 600, "end_minute": 500}]}]+days()[1:]]
        with patch("solo_setup.connect") as db:
            for draft in invalid:
                with self.subTest(draft=draft), self.assertRaises(ValueError):
                    save_schedule("unused", POLICY, draft, "admin:test")
            db.assert_not_called()


class SoleMasterDialog(CallbackGateway):
    def __init__(self, masters):
        super().__init__("unused", POLICY, 123, "synthetic", "synthetic")
        self.masters = masters
        self.state, self.draft = "service", {"id": "synthetic-draft"}

    def _load(self, vk_id):
        return self.state, copy.deepcopy(self.draft)

    def _save(self, vk_id, state, draft):
        self.state, self.draft = state, copy.deepcopy(draft)

    def _rows(self, sql, args=()):
        return self.masters if "FROM masters" in sql else [{"id": 1, "name": "Услуга"}]


class SoleMasterVK(unittest.TestCase):
    def act(self, masters):
        gateway = SoleMasterDialog(masters)
        now = datetime(2026, 10, 7, 6, tzinfo=UTC)
        with patch("vk_gateway.connect", return_value=Mock()), \
                patch("vk_gateway.get_available_days", return_value=["2026-10-08"]) as available:
            result = gateway._process(99, "", {"cmd": "service", "value": 1, "draft": "synthetic-draft"}, now)
        return gateway, result, available

    def test_one_eligible_master_goes_directly_to_real_available_days(self):
        gateway, result, available = self.act([{"id": 5, "name": "Анна"}])
        self.assertEqual(gateway.state, "date")
        self.assertEqual(gateway.draft["master_id"], 5)
        self.assertEqual(gateway.draft["master_name"], "Анна")
        self.assertIn("2026-10-08", gateway.draft["offered_days"])
        available.assert_called_once()
        self.assertNotIn("master", [button["payload"]["cmd"] for button in result[1]])

    def test_multiple_eligible_masters_preserve_choice(self):
        gateway, result, available = self.act([{"id": 5, "name": "Анна"}, {"id": 9, "name": "Елена"}])
        self.assertEqual(gateway.state, "master")
        self.assertEqual(result[0], "Выберите мастера")
        self.assertEqual([b["payload"]["value"] for b in result[1]], [5, 9])
        available.assert_not_called()

    def test_no_eligible_master_returns_unavailable_without_windows(self):
        gateway, result, available = self.act([])
        self.assertEqual(gateway.state, "home")
        self.assertIn("нет доступных мастеров", result[0])
        available.assert_not_called()


@unittest.skipUnless(RAW_URL, "SOLO_TEST_DATABASE_URL absent; SQL acceptance not executed")
class SoloPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(RAW_URL).path.endswith("_test"):
            raise ValueError("Use a fresh disposable *_test database")
        with closing(connect(RAW_URL)) as db:
            if db.execute("SELECT to_regclass('__APP_SCHEMA__.services')").fetchone()[0]:
                raise ValueError("Solo test database must be fresh")
            migrate(db)
        cls.admin = create_or_update_admin(RAW_URL, "solo_test", PASSWORD)

    def setUp(self):
        self.now = datetime.now(UTC)
        self.zone = ZoneInfo(POLICY["timezone"])
        self.day = (self.now.astimezone(self.zone)+timedelta(days=1)).date()
        self.start = datetime(self.day.year, self.day.month, self.day.day, 10, tzinfo=self.zone)
        self.actor = "admin:solo-test"
        with closing(connect(RAW_URL)) as db:
            db.execute("BEGIN IMMEDIATE")
            sid = provision_salon(db, "Соло "+uuid4().hex, POLICY)
            grant_membership(db, self.admin, sid)
            db.commit()
        self.scope = SalonScope(RAW_URL, sid)

    def enable(self, **extra):
        return save_setup(self.scope, POLICY, {"mode": "solo", "master_name": "Анна", **extra}, self.actor, self.now)

    def service(self, name="Услуга"):
        return create_service(self.scope, name, 60, self.actor)["id"]

    def team(self, master_links=True, room_links=True, master_days=None, room_days=None):
        sid = self.service()
        mid = save_resource(self.scope, "master", None, "Анна", [sid] if master_links else [], True, self.actor)["id"]
        rid = save_resource(self.scope, "room", None, "Кабинет", [sid] if room_links else [], True, self.actor)["id"]
        for kind, resource, draft in (("master", mid, master_days or days()), ("room", rid, room_days or days())):
            update_weekly_schedule_batch(self.scope, POLICY, kind, resource, draft, self.actor)
        return sid, mid, rid

    def state(self):
        tables = ("services", "masters", "rooms", "master_services", "room_services", "work_intervals",
                  "bookings", "booking_history", "message_outbox", "admin_audit_log")
        with closing(connect(self.scope)) as db:
            return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                    for table in tables}

    def test_new_solo_creates_one_resource_each_and_retry_keeps_identity(self):
        sid = self.service()
        first = self.enable()
        repeated = self.enable(master_name="Анна новая")
        self.assertEqual((first["master_id"], first["room_id"]), (repeated["master_id"], repeated["room_id"]))
        self.assertEqual(repeated["master_name"], "Анна новая")
        self.assertFalse(any(repeated["differences"].values()))
        with closing(connect(self.scope)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM masters").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM rooms").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT service_id FROM master_services").fetchone()[0], sid)
            self.assertEqual(db.execute("SELECT service_id FROM room_services").fetchone()[0], sid)
        self.assertEqual(snapshot(self.scope, POLICY, self.day)["solo_setup"], repeated)

    def test_expected_null_lost_success_is_observable_without_duplicate_resources(self):
        request = {"mode": "solo", "master_name": "Анна", "expected_master_id": None, "expected_room_id": None}
        first = save_setup(self.scope, POLICY, request, self.actor)
        before = self.state()
        with self.assertRaises(AdminConflict):
            save_setup(self.scope, POLICY, request, self.actor)
        self.assertEqual(before, self.state())
        observed = get_setup(self.scope)
        self.assertEqual(observed["mode"], "solo")
        self.assertEqual(observed["master_name"], request["master_name"])
        self.assertEqual((first["master_id"], first["room_id"]),
                         (observed["master_id"], observed["room_id"]))

    def test_stale_expected_ids_cannot_write_to_a_replaced_single_resource(self):
        old = self.enable()
        expected = {"expected_master_id": old["master_id"], "expected_room_id": old["room_id"]}
        save_setup(self.scope, POLICY, {"mode": "team", **expected}, self.actor)
        save_resource(self.scope, "master", old["master_id"], "Анна", [], False, self.actor)
        save_resource(self.scope, "master", None, "Елена", [], True, self.actor)
        replacement = save_setup(self.scope, POLICY, {"mode": "solo", "master_name": "Елена"}, self.actor)
        self.assertNotEqual(replacement["master_id"], old["master_id"])
        before = self.state()
        for operation in (
            lambda: save_setup(self.scope, POLICY, {"mode": "solo", "master_name": "Анна", **expected}, self.actor),
            lambda: save_schedule(self.scope, POLICY, days(), self.actor, expectations=expected)):
            with self.assertRaises(AdminConflict):
                operation()
            self.assertEqual(before, self.state())

    def test_existing_service_differences_require_explicit_reconciliation(self):
        sid, mid, rid = self.team(room_links=False)
        before = self.state()
        view = get_setup(self.scope)
        self.assertTrue(view["differences"]["services"])
        self.assertEqual(view["service_differences"]["room_missing_ids"], [sid])
        with self.assertRaises(AdminConflict):
            self.enable()
        self.assertEqual(before, self.state())
        result = self.enable(reconcile_services=True)
        self.assertEqual(result["master_id"], mid)
        self.assertEqual(result["room_id"], rid)
        self.assertFalse(result["differences"]["services"])

    def test_existing_schedule_difference_needs_source_and_copies_both(self):
        self.team(room_days=days(600, 1020))
        before = self.state()
        with self.assertRaises(AdminConflict):
            self.enable()
        self.assertEqual(before, self.state())
        result = self.enable(hours_source="room")
        self.assertEqual(result["master_days"], days(600, 1020))
        self.assertEqual(result["room_days"], days(600, 1020))

    def test_reconciliation_never_cancels_a_booking_outside_selected_schedule(self):
        sid, mid, rid = self.team(master_days=days(540, 1080), room_days=days(600, 1080))
        booking = create_manual_booking(self.scope, POLICY, "+79990101001", sid, mid,
                                        self.start.replace(hour=17), uuid4().hex, self.actor, self.now)
        # Tighten only the other resource's actual weekly data as a legacy writer.
        with closing(connect(self.scope)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE work_intervals SET end_minute=960 WHERE master_id=?", (mid,))
            db.commit()
        before = self.state()
        with self.assertRaises(AdminConflict) as conflict:
            self.enable(hours_source="master")
        self.assertEqual(conflict.exception.affected_booking_ids, [booking["id"]])
        self.assertEqual(before, self.state())

    def test_service_creation_and_reactivation_assign_both_in_same_transaction(self):
        old = self.service("Отключённая")
        update_service(self.scope, old, "Отключённая", 60, False, self.actor)
        result = self.enable()
        new = self.service("Новая")
        update_service(self.scope, old, "Отключённая", 60, True, self.actor)
        with closing(connect(self.scope)) as db:
            for kind in ("master", "room"):
                linked = {row[0] for row in db.execute(f"SELECT service_id FROM {kind}_services WHERE {kind}_id=?",
                                                     (result[kind+"_id"],))}
                self.assertEqual(linked, {new, old})

    def test_cardinality_change_rejects_service_and_schedule_without_partial_writes(self):
        self.enable()
        with closing(connect(self.scope)) as db:
            db.execute("INSERT INTO masters(name,active) VALUES ('Legacy extra',1)")
        before = self.state()
        for operation in (lambda: self.service(), lambda: save_schedule(self.scope, POLICY, days(), self.actor)):
            with self.assertRaises(AdminConflict):
                operation()
            self.assertEqual(before, self.state())

    def test_team_expansion_preserves_resources_links_schedules_and_bookings(self):
        setup = self.enable()
        sid = self.service()
        save_schedule(self.scope, POLICY, days(), self.actor)
        create_manual_booking(self.scope, POLICY, "+79990101002", sid, setup["master_id"],
                              self.start, uuid4().hex, self.actor, self.now)
        before = self.state()
        result = save_setup(self.scope, POLICY, {"mode": "team"}, self.actor)
        after = self.state()
        self.assertEqual(result["mode"], "team")
        for table in before:
            if table != "admin_audit_log":
                self.assertEqual(before[table], after[table])
        save_resource(self.scope, "master", None, "Второй мастер", [sid], True, self.actor)
        self.assertFalse(get_setup(self.scope)["eligible"])
        with self.assertRaises(AdminConflict):
            self.enable()

    def test_solo_guards_individual_graph_resource_changes_and_link_removal(self):
        setup = self.enable()
        self.service()
        mid, rid = setup["master_id"], setup["room_id"]
        before = self.state()
        operations = [
            lambda: save_resource(self.scope, "master", None, "Второй", [], True, self.actor),
            lambda: save_resource(self.scope, "room", rid, "Кабинет", [], False, self.actor),
            lambda: save_resource(self.scope, "master", mid, "Анна", [], True, self.actor),
            lambda: update_weekly_schedule_batch(self.scope, POLICY, "master", mid, days(), self.actor),
            lambda: update_weekly_schedule(self.scope, POLICY, "room", rid, 0, 540, 1080, self.actor)]
        for operation in operations:
            with self.assertRaises(AdminConflict):
                operation()
            self.assertEqual(before, self.state())

    def test_common_schedule_preserves_booking_and_conflict_rolls_back_both(self):
        setup = self.enable()
        sid = self.service()
        save_schedule(self.scope, POLICY, days(), self.actor)
        booking = create_manual_booking(self.scope, POLICY, "+79990101003", sid, setup["master_id"],
                                        self.start, uuid4().hex, self.actor, self.now)
        before = self.state()
        for now in (self.now, self.start.astimezone(UTC)+timedelta(minutes=15)):
            with self.assertRaises(AdminConflict) as conflict:
                save_schedule(self.scope, POLICY, days(660, 1080), self.actor, now)
            self.assertEqual(conflict.exception.affected_booking_ids, [booking["id"]])
            self.assertEqual(before, self.state())
        result = save_schedule(self.scope, POLICY, days(540, 1140), self.actor)
        self.assertEqual(result["master_days"], result["room_days"])
        self.assertEqual(self.state()["bookings"], before["bookings"])

    def test_failure_writing_room_schedule_rolls_back_master_schedule(self):
        self.enable()
        save_schedule(self.scope, POLICY, days(), self.actor)
        before = self.state()
        original = connect
        def failed(path):
            db = original(path)
            execute_many = db.executemany
            def write(sql, args):
                if "INSERT INTO work_intervals(room_id" in sql:
                    raise RuntimeError("synthetic second-resource failure")
                return execute_many(sql, args)
            db.executemany = write
            return db
        with patch("solo_setup.connect", side_effect=failed), self.assertRaises(RuntimeError):
            save_schedule(self.scope, POLICY, days(600, 1140), self.actor)
        self.assertEqual(before, self.state())

    def test_dated_exceptions_and_resource_blocks_remain_effective(self):
        setup = self.enable()
        sid = self.service()
        save_schedule(self.scope, POLICY, days(), self.actor)
        with closing(connect(self.scope)) as db:
            db.execute("INSERT INTO work_intervals(master_id,local_date,mode,start_minute,end_minute) VALUES (?,?,'closed',600,660)",
                       (setup["master_id"], self.day.isoformat()))
            db.execute("INSERT INTO resource_blocks(room_id,start_utc,end_utc,reason,created_by) VALUES (?,?,?,?,?)",
                       (setup["room_id"], stamp(self.start.replace(hour=11)), stamp(self.start.replace(hour=12)), "test", self.actor))
        result = save_schedule(self.scope, POLICY, days(540, 1140), self.actor)
        self.assertEqual(len(result["schedule_exceptions"]["master"]), 1)
        with closing(connect(self.scope)) as db:
            slots = get_available_slots(db, POLICY, sid, setup["master_id"], self.day, self.now)
        self.assertNotIn(stamp(self.start), slots)
        self.assertNotIn(stamp(self.start.replace(hour=11)), slots)
        self.assertIn(stamp(self.start.replace(hour=12)), slots)

    def test_internal_metadata_cannot_be_changed_through_generic_constructor(self):
        self.enable()
        with closing(connect(self.scope)) as db:
            metadata = constructor_snapshot(db)
            typ = next(t for t in metadata["types"] if t["code"] == "salon_settings")
            param = next(p for p in typ["parameters"] if p["code"] == "constructor_mode")
            entity = next(e for e in metadata["entities"] if e["entity_type_id"] == typ["id"])
            for operation in (
                lambda: save_entity(db, {"id": entity["id"], "values": {"constructor_mode": "team"}}, self.actor),
                lambda: save_parameter(db, {"id": param["id"], "label": "hidden"}, self.actor),
                lambda: delete_parameter(db, param["id"], self.actor)):
                with self.assertRaises(ConstructorError):
                    operation()
        self.assertEqual(get_setup(self.scope)["mode"], "solo")

    def test_shared_master_identity_survives_enabling_renaming_and_expansion(self):
        sid, mid, _ = self.team()
        with closing(connect(RAW_URL)) as db:
            db.execute("BEGIN IMMEDIATE")
            second_id = provision_salon(db, "Второй "+uuid4().hex, POLICY)
            grant_membership(db, self.admin, second_id)
            db.commit()
        second = SalonScope(RAW_URL, second_id)
        second_service = create_service(second, "Услуга", 60, self.actor)["id"]
        attached = attach_master(second, self.admin, self.scope.salon_id, mid, [second_service])["id"]
        save_setup(second, POLICY, {"mode": "solo", "master_name": "Профиль Анны"}, self.actor)
        with closing(connect(self.scope)) as db:
            identity = db.execute("SELECT shared_master_id FROM __APP_SCHEMA__.masters WHERE salon_id=? AND id=?",
                                  (self.scope.salon_id, mid)).fetchone()[0]
        with closing(connect(second)) as db:
            self.assertEqual(db.execute("SELECT shared_master_id FROM __APP_SCHEMA__.masters WHERE salon_id=? AND id=?",
                                        (second.salon_id, attached)).fetchone()[0], identity)

    def test_http_auth_csrf_scope_and_reserved_mode_protection(self):
        httpd = server(RAW_URL, POLICY, "synthetic-solo-csrf-secret-32-bytes", port=0, secure_cookie=False)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_port}"
        client = build_opener(HTTPCookieProcessor(CookieJar()))
        def call(path, body=None, headers=None):
            request = Request(base+path, data=None if body is None else json.dumps(body).encode(),
                              headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with client.open(request) as response:
                    return response.status, json.load(response)
            except HTTPError as response:
                return response.code, json.load(response)
        self.assertEqual(call("/api/solo-setup")[0], 401)
        status, login = call("/api/login", {"username": "solo_test", "password": PASSWORD})
        self.assertEqual(status, 200)
        headers = {"X-Salon-Id": str(self.scope.salon_id), "X-CSRF-Token": login["csrf_token"]}
        self.assertEqual(call("/api/solo-setup", {"mode": "solo", "master_name": "Анна"},
                              {"X-Salon-Id": str(self.scope.salon_id)})[0], 403)
        self.assertEqual(call("/api/solo-setup", headers=headers)[1]["mode"], "team")
        self.assertEqual(call("/api/solo-setup", {"mode": "solo", "master_name": "Анна"}, headers)[0], 200)
        self.assertEqual(call("/api/solo-schedule", {"days": days()}, headers)[0], 200)
        self.assertEqual(call("/api/solo-setup", headers={**headers, "X-Salon-Id": "99999999"})[0], 403)
        self.assertEqual(call("/api/snapshot?date="+self.day.isoformat(), headers=headers)[1]["solo_setup"]["mode"], "solo")
        metadata = call("/api/constructor", headers=headers)[1]
        typ = next(t for t in metadata["types"] if t["code"] == "salon_settings")
        entity = next(e for e in metadata["entities"] if e["entity_type_id"] == typ["id"])
        self.assertEqual(call("/api/constructor/entities/"+str(entity["id"]),
                              {"values": {"constructor_mode": "team"}}, headers)[0], 422)


if __name__ == "__main__":
    unittest.main()

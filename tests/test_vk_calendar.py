"""Calendar dialog contracts, without network or production data."""

import copy
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from booking_core import BookingConflict, get_available_days, get_available_slots, stamp
from vk_gateway import CallbackGateway
from vk_sender import vk_keyboard

UTC = timezone.utc
POLICY = json.loads(Path(__file__).resolve().parents[1].joinpath("starter_data.json").read_text())["policy"]
NOW = datetime(2026, 10, 6, 6, tzinfo=UTC)


class Dialog(CallbackGateway):
    def __init__(self, policy=None):
        super().__init__("unused", policy or POLICY, 123, "synthetic", "synthetic")
        self.state = "master"
        self.draft = {"id": "synthetic-draft", "service_id": 1, "service_name": "Услуга"}

    def _load(self, vk_id):
        return self.state, copy.deepcopy(self.draft)

    def _save(self, vk_id, state, draft):
        self.state, self.draft = state, copy.deepcopy(draft)

    def _rows(self, sql, args=()):
        return [{"id": 9, "name": "Мастер", "service_id": 1, "master_id": 2,
                 "service_name_snapshot": "Услуга", "master_name_snapshot": "Мастер"}]


class CalendarDialog(unittest.TestCase):
    def setUp(self):
        self.gateway = Dialog()
        self.days = ["2026-10-07", "2026-10-09"]
        self.slots = {day: [stamp(datetime.fromisoformat(day).replace(hour=6, tzinfo=UTC) + timedelta(minutes=30 * i))
                            for i in range(18)] for day in self.days}
        self.database = Mock()
        self.connect = patch("vk_gateway.connect", return_value=self.database).start()
        self.available_days = patch("vk_gateway.get_available_days", side_effect=lambda *a, **kw: list(self.days)).start()
        self.available_slots = patch("vk_gateway.get_available_slots",
                                     side_effect=lambda db, policy, sid, mid, day, now, **kw: list(self.slots.get(day.isoformat(), []))).start()
        self.addCleanup(patch.stopall)

    def act(self, command=None, value=None, text="", draft=None):
        payload = {} if command is None else {"cmd": command, "value": value,
                                             "draft": draft or self.gateway.draft["id"]}
        return self.gateway._process(1001, text, payload, NOW)

    @staticmethod
    def options(reply, command):
        return [b for b in reply[1] if b["payload"]["cmd"] == command]

    def start(self):
        return self.act("master", 2)

    def choose_day(self, value="2026-10-07"):
        self.start()
        return self.act("day", value)

    def test_master_offers_only_available_days_then_local_times(self):
        reply = self.start()
        self.assertEqual([b["payload"]["value"] for b in self.options(reply, "day")], self.days)
        self.assertIn("Ср, 07.10", [b["label"] for b in reply[1]])
        reply = self.act("day", self.days[0])
        self.assertIn("07.10.2026", reply[0])
        self.assertEqual(self.options(reply, "time")[0]["label"], "09:00")
        self.assertEqual(self.gateway.state, "time")

    def test_all_days_and_times_are_reachable_and_can_go_back(self):
        self.days = [(date(2026, 10, 7) + timedelta(days=i)).isoformat() for i in range(22)]
        reply = self.start()
        found = []
        while True:
            found.extend(b["payload"]["value"] for b in self.options(reply, "day"))
            next_buttons = [b for b in self.options(reply, "days") if b["label"] == "Позже"]
            if not next_buttons:
                break
            reply = self.act("days", next_buttons[0]["payload"]["value"])
        self.assertEqual(found, self.days)
        reply = self.act("days", 0)
        reply = self.act("day", self.days[0])
        found = []
        for page in range(3):
            reply = self.act("times", page)
            found.extend(b["payload"]["value"] for b in self.options(reply, "time"))
        self.assertEqual(found, self.slots[self.days[0]])
        self.assertEqual(len(found), 18)
        self.act("days", 0)
        self.assertEqual(self.gateway.state, "date")
        self.assertNotIn("local_date", self.gateway.draft)
        self.assertNotIn("offered_slots", self.gateway.draft)

    def test_keyboard_pages_have_two_options_per_row_and_separate_navigation(self):
        self.days = [(date(2026, 10, 7) + timedelta(days=i)).isoformat() for i in range(24)]
        first_days = self.start()
        middle_days = self.act("days", 1)
        self.act("days", 0)
        first_times = self.act("day", self.days[0])
        middle_times = self.act("times", 1)
        self.assertEqual(len(self.options(first_times, "time")), 8)
        self.assertEqual(len(self.options(middle_times, "time")), 8)
        for reply in (first_days, middle_days, first_times, middle_times):
            keyboard = json.loads(vk_keyboard(reply[1]))
            self.assertTrue(keyboard["inline"])
            self.assertLessEqual(len(keyboard["buttons"]), 6)
            self.assertTrue(all(len(row) <= 2 for row in keyboard["buttons"]))
            self.assertEqual(sum(map(len, keyboard["buttons"])), len(reply[1]))
            for row in keyboard["buttons"]:
                for item in row:
                    self.assertEqual(item["action"]["type"], "text")
                    self.assertIn("cmd", json.loads(item["action"]["payload"]))

    def test_invalid_page_values_are_bounded_without_losing_options(self):
        self.start()
        for value in (-10, 99999, True, "2", {}, None):
            with self.subTest(value=value):
                reply = self.act("days", value)
                self.assertEqual(len(self.options(reply, "day")), 2)
        self.act("day", self.days[0])
        reply = self.act("times", 99999)
        self.assertEqual(len(self.options(reply, "time")), 2)
        self.assertEqual(self.gateway.draft["time_page"], 2)

    def test_empty_calendar_can_be_refreshed_when_new_day_appears(self):
        self.days = []
        reply = self.start()
        self.assertIn("свободных дней нет", reply[0])
        self.assertEqual(self.options(reply, "day"), [])
        self.days = ["2026-10-09"]
        self.assertEqual(len(self.options(self.act("days", 0), "day")), 1)

    def test_day_rechecked_and_disappeared_day_returns_other_days(self):
        self.start()
        self.slots[self.days[0]] = []
        self.days.pop(0)
        reply = self.act("day", "2026-10-07")
        self.assertEqual(self.gateway.state, "date")
        self.assertEqual([b["payload"]["value"] for b in self.options(reply, "day")], ["2026-10-09"])

    def test_invalid_day_payload_and_legacy_text_date_are_safe(self):
        self.start()
        for value in ("bad", "2026-12-25", {}, None):
            self.assertEqual(len(self.options(self.act("day", value), "day")), 2)
        reply = self.act(text="07.10.2026")
        self.assertTrue(self.options(reply, "time"))
        self.assertEqual(self.gateway.state, "time")

    def test_time_rechecked_before_phone_and_unoffered_values_rejected(self):
        self.choose_day()
        slot = self.slots[self.days[0]][0]
        self.slots[self.days[0]].remove(slot)
        reply = self.act("time", slot)
        self.assertIn("уже недоступно", reply[0])
        self.assertEqual(self.gateway.state, "time")
        for value in (None, {}, "2030-01-01T10:00:00Z", self.slots[self.days[0]][-1]):
            with self.subTest(value=value):
                self.act("time", value)
                self.assertEqual(self.gateway.state, "time")
        self.act("time", self.gateway.draft["offered_slots"][0])
        self.assertEqual(self.gateway.state, "phone")

    def test_old_time_button_from_another_page_or_day_does_not_advance(self):
        self.choose_day()
        old_slot = self.gateway.draft["offered_slots"][0]
        self.act("times", 1)
        self.act("time", old_slot)
        self.assertEqual(self.gateway.state, "time")
        self.act("days", 0)
        self.act("day", self.days[1])
        self.act("time", old_slot)
        self.assertEqual(self.gateway.state, "time")

    def test_old_draft_button_does_not_change_state(self):
        self.start()
        before = copy.deepcopy(self.gateway.draft)
        reply = self.act("day", self.days[0], draft="older-draft")
        self.assertIn("устарела", reply[0])
        self.assertEqual(self.gateway.draft, before)

    def test_legacy_time_dialog_without_date_recovers_to_calendar(self):
        self.start()
        self.gateway.state = "time"
        self.act("time", self.slots[self.days[0]][0])
        self.assertEqual(self.gateway.state, "date")

    def test_booking_confirmation_and_conflict_return_current_options(self):
        self.choose_day()
        slot = self.gateway.draft["offered_slots"][0]
        self.act("time", slot)
        self.act(text="+79990001111")
        self.assertEqual(self.gateway.state, "confirm")
        self.slots[self.days[0]].remove(slot)
        with patch("vk_gateway.confirm_booking", side_effect=BookingConflict("synthetic")):
            reply = self.act("confirm")
        self.assertEqual(self.gateway.state, "time")
        self.assertNotIn(slot, self.gateway.draft["offered_slots"])
        self.assertIn("Не удалось записаться", reply[0])
        self.act("time", self.gateway.draft["offered_slots"][0])
        self.act(text="+79990001111")
        current_draft_id = self.gateway.draft["id"]
        with patch("vk_gateway.confirm_booking", return_value={"id": 44}) as confirm:
            reply = self.act("confirm")
        self.assertEqual(reply[2], 44)
        self.assertEqual(self.gateway.state, "home")
        self.assertEqual(confirm.call_args.args[7], "vk-confirm:" + current_draft_id)

    def test_old_confirmation_cannot_confirm_another_date(self):
        self.choose_day()
        self.act("time", self.gateway.draft["offered_slots"][0])
        first_confirm = self.act(text="+79990001111")[1][0]["payload"]
        self.act("days", 0)
        self.act("day", self.days[1])
        self.act("time", self.gateway.draft["offered_slots"][0])
        second_confirm = self.act(text="+79990001111")[1][0]["payload"]
        self.assertNotEqual(first_confirm["draft"], second_confirm["draft"])
        before = copy.deepcopy(self.gateway.draft)
        with patch("vk_gateway.confirm_booking") as confirm:
            reply = self.gateway._process(1001, "", first_confirm, NOW)
        confirm.assert_not_called()
        self.assertIn("устарела", reply[0])
        self.assertEqual(self.gateway.state, "confirm")
        self.assertEqual(self.gateway.draft, before)

    def test_old_reschedule_confirmation_cannot_move_to_another_date(self):
        self.gateway.state = "my"
        self.act("reschedule", 9)
        self.act("day", self.days[0])
        first_confirm = self.act("time", self.gateway.draft["offered_slots"][0])[1][0]["payload"]
        self.act("days", 0)
        self.act("day", self.days[1])
        second_confirm = self.act("time", self.gateway.draft["offered_slots"][0])[1][0]["payload"]
        self.assertNotEqual(first_confirm["draft"], second_confirm["draft"])
        with patch("vk_gateway.reschedule_booking") as reschedule:
            reply = self.gateway._process(1001, "", first_confirm, NOW)
        reschedule.assert_not_called()
        self.assertIn("устарела", reply[0])
        self.assertEqual(self.gateway.state, "reschedule_confirm")

    def test_confirmation_calls_atomic_core_directly_to_preserve_replay(self):
        self.choose_day()
        slot = self.gateway.draft["offered_slots"][0]
        self.act("time", slot)
        self.act(text="+79990001111")
        self.available_slots.reset_mock()
        with patch("vk_gateway.confirm_booking", return_value={"id": 44}):
            self.act("confirm")
        self.available_slots.assert_not_called()

    def test_reschedule_uses_calendar_and_preserves_existing_booking_id(self):
        self.gateway.state = "my"
        reply = self.act("reschedule", 9)
        self.assertTrue(self.options(reply, "day"))
        self.assertEqual(self.available_days.call_args.kwargs["except_id"], 9)
        self.act("day", self.days[0])
        self.assertEqual(self.available_slots.call_args.kwargs["except_id"], 9)
        self.act("time", self.gateway.draft["offered_slots"][0])
        self.assertEqual(self.gateway.state, "reschedule_confirm")
        with patch("vk_gateway.reschedule_booking", side_effect=BookingConflict("synthetic")):
            self.act("reschedule_confirm")
        self.assertEqual(self.gateway.state, "reschedule_time")
        self.assertEqual(self.gateway.draft["booking_id"], 9)


class AvailabilityDates(unittest.TestCase):
    def test_uses_local_today_same_day_policy_and_inclusive_horizon(self):
        now = datetime(2026, 10, 6, 22, tzinfo=UTC)  # Oct 7 in Moscow.
        policy = {**POLICY, "booking_horizon_days": 2, "same_day_allowed": False}
        with patch("booking_core._candidate") as candidate:
            days = get_available_days(None, policy, 1, 2, now)
        self.assertEqual(days, ["2026-10-08", "2026-10-09"])
        self.assertEqual(candidate.call_count, 2)  # Stops on each day's first free slot.
        self.assertEqual(get_available_days(None, {**policy, "booking_horizon_days": 0}, 1, 2, now), [])

    def test_days_reuse_slot_rejections_and_exclude_empty_days(self):
        policy = {**POLICY, "booking_horizon_days": 2}
        def candidate(db, policy, sid, mid, start, now, **kwargs):
            local = start.astimezone(__import__("zoneinfo").ZoneInfo(policy["timezone"]))
            if local.day != 8 or local.hour != 10:
                raise BookingConflict("No room, blocked, busy, or notice")
        with patch("booking_core._candidate", side_effect=candidate):
            self.assertEqual(get_available_days(None, policy, 1, 2, NOW), ["2026-10-08"])
            slots = get_available_slots(None, policy, 1, 2, date(2026, 10, 8), NOW)
            self.assertEqual(slots, ["2026-10-08T07:00:00Z", "2026-10-08T07:30:00Z"])

    def test_exclusion_forwarded_and_naive_now_rejected(self):
        with patch("booking_core._candidate") as candidate:
            get_available_days(None, {**POLICY, "booking_horizon_days": 0}, 1, 2, NOW, except_id=9)
        self.assertEqual(candidate.call_args.kwargs["except_id"], 9)
        with self.assertRaises(ValueError):
            get_available_days(None, POLICY, 1, 2, NOW.replace(tzinfo=None))

    def test_slots_still_handle_dst_gaps_and_fold_without_duplicate_instants(self):
        policy = {**POLICY, "timezone": "Europe/Berlin"}
        with patch("booking_core._candidate"):
            spring = get_available_slots(None, policy, 1, 2, date(2026, 3, 29), NOW)
            autumn = get_available_slots(None, policy, 1, 2, date(2026, 10, 25), NOW)
        self.assertEqual(len(spring), 46)
        self.assertEqual(len(autumn), 50)
        self.assertEqual(len(set(autumn)), 50)


if __name__ == "__main__":
    unittest.main()

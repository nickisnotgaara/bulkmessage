import sys
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, ".")
from bulkmessage import config, schedule, sender

config.now_tz = lambda: datetime(2026, 10, 12, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
print("сейчас          :", config.now_tz().strftime("%H:%M"))
print("within_safe_hrs :", sender._within_safe_hours("telegram"))
print("seconds_left tg :", sender._phase_seconds_left("telegram"))
print("slot_count      :", sender._personal_slot_count())
print("slots_in_window :", sender._personal_slots_in_window())
sec = sender._phase_seconds_left("telegram")
print("build(60, %.0f) : %d" % (sec, len(schedule.build_slot_schedule(60, sec, min_gap_seconds=900, jitter_max=1.4))))

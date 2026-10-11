import sys

sys.path.insert(0, "/opt/bulkmessage")
try:
    from bulkmessage import schedule
    print("schedule OK:", [n for n in dir(schedule) if not n.startswith("_")][:6])
except Exception as e:
    print("schedule FAIL:", type(e).__name__, e)

from bulkmessage import sender, probe, config
print("sender OK")
print("probe OK")
print("select_probe_candidates:", hasattr(sender, "select_probe_candidates"))
print("PROBE_MAX_PER_RUN:", config.PROBE_MAX_PER_RUN)
print("MAX_ATTEMPTS_PER_DAY:", config.MAX_ATTEMPTS_PER_DAY)
print("WABA_RUN_LIMIT:", config.WABA_RUN_LIMIT)
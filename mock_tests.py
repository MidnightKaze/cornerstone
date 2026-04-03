"""
mock_tests.py
=============
Unit tests for the Smart Power Scheduler.
Runs on a regular PC (CPython) — no Pico or INA219 hardware needed.

Run with:
    python mock_tests.py
"""

# ── Compatibility shims so the code runs outside MicroPython ──────────────────
import sys
import types

# Stub out 'machine' so imports don't fail on PC
machine_mod = types.ModuleType("machine")
class _Pin:
    OUT = 1
    def __init__(self, *a, **kw): pass
    def value(self, v=None): pass
class _I2C:
    def __init__(self, *a, **kw): pass
machine_mod.Pin  = _Pin
machine_mod.I2C  = _I2C
sys.modules["machine"] = machine_mod

# Stub out 'utime'
utime_mod = types.ModuleType("utime")
import time as _time
utime_mod.ticks_ms   = lambda: int(_time.time() * 1000)
utime_mod.ticks_diff = lambda a, b: a - b
utime_mod.sleep      = _time.sleep
sys.modules["utime"] = utime_mod

# Stub out 'ujson' with standard json
import json as _json
ujson_mod = types.ModuleType("ujson")
ujson_mod.dump  = _json.dump
ujson_mod.dumps = _json.dumps
ujson_mod.load  = _json.load
ujson_mod.loads = _json.loads
sys.modules["ujson"] = ujson_mod

# ── Now safe to import our module ─────────────────────────────────────────────
# We import the classes directly by exec-ing main.py into a namespace so we
# don't need to restructure the source file.
with open("dev.py", "r") as f:
    src = f.read()
ns = {}
exec(src, ns)

INA219   = ns["INA219"]
Scheduler = ns["Scheduler"]
Relay    = ns["Relay"]
SCHEDULE_SLOTS   = ns["SCHEDULE_SLOTS"]
ON_WATT_THRESHOLD = ns["ON_WATT_THRESHOLD"]
MAX_VOTE_DAYS    = ns["MAX_VOTE_DAYS"]
LEARNING_DAYS    = ns["LEARNING_DAYS"]

# ── Minimal test harness ──────────────────────────────────────────────────────
passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))
        failed += 1

def section(title):
    print(f"\n{'─'*50}")
    print(f" {title}")
    print(f"{'─'*50}")


# ─────────────────────────────────────────────
# MOCK I2C BUS
# ─────────────────────────────────────────────
class MockI2C:
    """
    Simulates the I2C bus for the INA219.
    Pre-load register_data[reg] with the 16-bit value the chip would return.
    """
    def __init__(self, register_data=None):
        self.register_data = register_data or {}
        self._pending_reg  = None
        self.write_log     = []   # records every writeto call

    def writeto(self, addr, data):
        self.write_log.append((addr, bytes(data)))
        if len(data) == 1:
            self._pending_reg = data[0]   # register pointer set

    def readfrom(self, addr, n):
        val = self.register_data.get(self._pending_reg, 0)
        return bytes([(val >> 8) & 0xFF, val & 0xFF])

    def set_register(self, reg, value):
        self.register_data[reg] = value


# ─────────────────────────────────────────────
# INA219 DRIVER TESTS
# ─────────────────────────────────────────────
section("INA219 — _read_reg / _write_reg")

i2c = MockI2C()
ina = INA219(i2c)

# Init should write CONFIG and CALIB registers
regs_written = [entry[1][0] for entry in i2c.write_log]
test("CONFIG register written on init",  0x00 in regs_written)
test("CALIB register written on init",   0x05 in regs_written)

section("INA219 — bus_voltage()")

# Bus voltage register: raw value 6400 → (6400 >> 3) * 0.004 = 3.2 V
i2c.set_register(0x02, 6400)
v = ina.bus_voltage()
test("Bus voltage 3.2V decoded correctly", abs(v - 3.2) < 0.001,
     f"got {v:.4f}V")

# Zero volts
i2c.set_register(0x02, 0)
test("Bus voltage 0V", ina.bus_voltage() == 0.0)

# Higher voltage: 120V mains bus is outside INA219 range but let's check math
# raw = (24.0 / 0.004) << 3 = 48000
i2c.set_register(0x02, 48000)
v = ina.bus_voltage()
test("Bus voltage 24V decoded correctly", abs(v - 24.0) < 0.01,
     f"got {v:.4f}V")

section("INA219 — current_mA()")

# Positive current: raw = 1000 → 1000 * 0.1 = 100 mA
i2c.set_register(0x04, 1000)
c = ina.current_mA()
test("Positive current 100mA decoded", abs(c - 100.0) < 0.01,
     f"got {c:.2f}mA")

# Zero current
i2c.set_register(0x04, 0)
test("Zero current decoded", ina.current_mA() == 0.0)

# Negative current (two's complement): -500 mA
# In 16-bit two's complement: 65536 - 5000 = 60536
i2c.set_register(0x04, 60536)
c = ina.current_mA()
test("Negative current decoded correctly", abs(c - (-500.0)) < 0.1,
     f"got {c:.2f}mA, expected -500.0mA")

section("INA219 — read() power calculation")

# 3.2V at 500mA = 1.6W
i2c.set_register(0x02, 6400)   # 3.2V
i2c.set_register(0x04, 5000)   # 500mA
v, current, watts = ina.read()
test("Voltage correct in read()",  abs(v       - 3.2)  < 0.01, f"got {v:.3f}V")
test("Current correct in read()",  abs(current - 0.5)  < 0.001, f"got {current:.4f}A")
test("Power correct in read()",    abs(watts   - 1.6)  < 0.01, f"got {watts:.3f}W")

# Well above ON_WATT_THRESHOLD
i2c.set_register(0x02, 6400)   # 3.2V
i2c.set_register(0x04, 50000)  # 5000mA = 5A → 16W
_, _, watts = ina.read()
test("High draw correctly above ON threshold",
     watts >= ON_WATT_THRESHOLD, f"watts={watts:.2f}")

# Well below ON_WATT_THRESHOLD (standby / off)
i2c.set_register(0x02, 250)    # 0.125V
i2c.set_register(0x04, 100)    # 10mA → ~0.00125W
_, _, watts = ina.read()
test("Low draw correctly below ON threshold",
     watts < ON_WATT_THRESHOLD, f"watts={watts:.4f}")


# ─────────────────────────────────────────────
# SCHEDULE TESTS
# ─────────────────────────────────────────────
section("Schedule — learning phase")

sched = Schedule()
test("Starts unlocked",          not sched.locked)
test("Starts at 0 days seen",    sched.days_seen == 0)
test("All slots start OFF",      all(not s for s in sched.active))

# Feed enough days to lock
for day in range(LEARNING_DAYS):
    sched.mark_day_complete()

test("Locks after LEARNING_DAYS",         sched.locked)
test("days_seen matches LEARNING_DAYS",   sched.days_seen == LEARNING_DAYS)

section("Schedule — slot voting and ON/OFF logic")

sched = Schedule()

# Record slot 5 as ON for LEARNING_DAYS days, then build
for _ in range(LEARNING_DAYS):
    sched.record(5, True)
    sched.mark_day_complete()

test("Consistently ON slot is scheduled ON",  sched.should_be_on(5))
test("Untouched slot is scheduled OFF",       not sched.should_be_on(10))

# A slot seen only once should stay OFF (on_cnt < 2 guard)
sched2 = Schedule()
sched2.record(3, True)                        # only 1 hit
for _ in range(LEARNING_DAYS):
    sched2.mark_day_complete()
test("Single-hit slot stays OFF (false-positive guard)",
     not sched2.should_be_on(3))

# Slot ON half the time — right on the 50% boundary
sched3 = Schedule()
for i in range(LEARNING_DAYS):
    sched3.record(7, i % 2 == 0)             # ON on even days, OFF on odd
for _ in range(LEARNING_DAYS):
    sched3.mark_day_complete()
# 50% ratio + >= 2 hits should be ON
test("50% ON slot is scheduled ON",  sched3.should_be_on(7))

# Slot ON only 1 out of 4 days (25%) — should be OFF
sched4 = Schedule()
sched4.record(9, True)
for _ in range(LEARNING_DAYS - 1):
    sched4.record(9, False)
for _ in range(LEARNING_DAYS):
    sched4.mark_day_complete()
test("25% ON slot stays OFF",  not sched4.should_be_on(9))

section("Schedule — rolling purge (MAX_VOTE_DAYS)")

sched = Schedule()

# Fill slot 0 past the cap and verify total never exceeds MAX_VOTE_DAYS + 1
for i in range(MAX_VOTE_DAYS + 10):
    sched.record(0, True)
    on_cnt, total = sched.slot_votes[0]
    test(f"  total <= MAX_VOTE_DAYS after {i+1} records",
         total <= MAX_VOTE_DAYS,
         f"total={total:.2f}")
    if total > MAX_VOTE_DAYS:
        break   # no point continuing after first failure

# After many ON records slot should stay ON
test("Slot stays ON after purge cycles",
     sched.slot_votes[0][0] / sched.slot_votes[0][1] >= 0.5)

section("Schedule — maybe_update() decay")

sched = Schedule()
# Fill slot 1 with solid ON history
for _ in range(MAX_VOTE_DAYS):
    sched.record(1, True)
for _ in range(LEARNING_DAYS):
    sched.mark_day_complete()

before_total = sched.slot_votes[1][1]
sched.maybe_update()
after_total  = sched.slot_votes[1][1]

test("Votes decay after maybe_update()",
     after_total < before_total,
     f"before={before_total:.2f}, after={after_total:.2f}")
test("Slot remains ON after mild decay",
     sched.should_be_on(1))


# ─────────────────────────────────────────────
# RELAY TESTS
# ─────────────────────────────────────────────
section("Relay — stub mode (no pin)")

relay = Relay(None)
test("Relay initialises without pin",  relay._pin is None)
relay.on()
test("on()  sets state True",   relay._state is True)
relay.off()
test("off() sets state False",  relay._state is False)
relay.set(True)
test("set(True)  sets state True",   relay._state is True)
relay.set(False)
test("set(False) sets state False",  relay._state is False)

# No-op check — state shouldn't flip if already correct
relay._state = True
relay.on()
test("on() is no-op when already ON",  relay._state is True)


# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────
print(f"\n{'═'*50}")
print(f"  Results:  {passed} passed,  {failed} failed")
print(f"{'═'*50}\n")
sys.exit(0 if failed == 0 else 1)
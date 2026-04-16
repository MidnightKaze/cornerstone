# THIS IS WRITTEN IN MICROPYTHON, NOT PYTHON (ignore resolve error)

import machine
import utime
import ujson

# CHANGE ALL THE PINS WHEN WE WIRE

PIR_PIN = None
RELAY_PIN = None # Change it when we know it
RELAY_ON = 1
RELAY_OFF = 0
BUTTON_PIN = None # Change it when we know it

SAMPLE_INTERVAL = 30  # in seconds
LEARNING_DAYS = 4 
SCHEDULE_SLOTS = 48 # 48 30 minute time slots (2 days or 1140 minutes)
MOTION_TIMEOUT = 45 * 60

# It will keep two weeks worth of data before deleting the oldest slot
MAX_DAYS = 14

SCHEDULE_FILE = "schedule.json"
CLOCK_FILE = "clock.json" # Stops it from overriding the schedule lol
SAVE_TO_FLASH = 300 # Save to flash every 5 minutes (300 seconds)

OVERRIDE_TIME = 30 * 60 # It will override the button 30 minutes after it's pushed

# INA219 Drivers to read current and voltage
class PIR:
    def __init__(self, pin_num):
        self.pin = None
        if pin_num is not None:
            self.pin = machine.Pin(pin_num, machine.Pin.IN)

    def motion_detected(self):
        if self.pin is None:
            return False
        return self.pin.value() == 1
    
class Relay:
    def __init__(self, pin_num):
        self.pin = None
        self.state = False
        if pin_num is not None:
            self.pin = machine.Pin(pin_num, machine.Pin.OUT)
            self.apply()  # Ensure relay starts in off state

    def apply(self):
        if self.pin:
            self.pin.value(RELAY_ON if self.state else RELAY_OFF)

    def on(self):
        if not self.state:
            self.state = True
            self.apply()
            print("[RELAY] ON")

    def off(self):
        if self.state:
            self.state = False
            self.apply()
            print("[RELAY] OFF")

    def set(self, should_be_on):
        self.on() if should_be_on else self.off()

class ManualOverride:
    def __init__(self, button_pin):
        self.active = False
        self.override_state = False
        self.start_time = 0
        self._last_button = 1 # We will assume it's not pressed at all other times
        if button_pin is not None:
            self.button = machine.Pin(button_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        
    def update(self, now_seconds, current_relay_state):
        if self.button is not None:
            btn = self.button.value()
            if self._last_button == 1 and btn == 0: # Button pressed
                self.override_state = not current_relay_state # Toggle the override state
                self.active = True
                self.start_time = now_seconds
                print(f"[OVERRIDE] Button pressed. Override state: {'ON' if self.override_state else 'OFF'}")

            else:
                if self.active and (now_seconds - self.start_time) >= OVERRIDE_TIME:
                    self.active = False
                    print("[OVERRIDE] Override expired.")
            
            return self.active

class Scheduler:
    def __init__(self):
        self.slot = [[0,0,] for _ in range(SCHEDULE_SLOTS)]  # Each slot: [total_power, count, average_power]
        self.active = [False] * SCHEDULE_SLOTS  # Whether the relay should be on in this slot
        self.days_recorded = 0
        self.locked = False

    # Recording a sample and rolling concurrent purge
    def record_sample(self, slot, motion_seen):
        on_count, total = self.slot[slot]

        if total >= MAX_DAYS:
            evict_on = on_count / total # Evict if more than 50% of the samples had the relay on
            on_count = max(0.0, on_count - evict_on)
            total -= 1

        total += 1
        if motion_seen:
            on_count += 1

        self.slot[slot] = [on_count, total]

    def day_complete(self):
        self.days_recorded += 1
        print(f"[SCHEDULER] Day complete. Total days recorded: {self.days_recorded}")
        if self.days_recorded >= LEARNING_DAYS:
            self.build()
            self.locked = True

    def build(self):
        for i, (on_count, total) in enumerate(self.slot):
            if total == 0:
                self.active[i] = False
            else:
                self.active[i] = (on_count / total) >= 0.5  and (on_count >= 2)# Set active if more than 50% of samples had relay on

        on_count = sum(self.active)
        print(f"[SCHEDULER] Schedule built. Active slots: {on_count}/{SCHEDULE_SLOTS}")

    def is_active_slot(self, slot):
        if not self.locked:
            return True
        return self.active[slot]
    
    def maybe_update(self):
        if not self.locked:
            return
        for i in range(SCHEDULE_SLOTS):
            self.slot[i][0] = self.slot[i][0] * 0.75
            self.slot[i][1] = self.slot[i][1] * 0.75
        self.build()
    
    def save(self):
        data = {
            "votes": self.slot,
            "active": self.active,
            "days_recorded": self.days_recorded,
            "locked": self.locked
        }
        with open(SCHEDULE_FILE, "w") as f:
            ujson.dump(data, f)

    def load(self):
        try:
            with open(SCHEDULE_FILE, "r") as f:
                data = ujson.load(f)
            self.slot_votes = data["votes"]
            self.active = data["active"]
            self.days_recorded = data["days_recorded"]
            self.locked = data["locked"]
            print (f"[SCHEDULER] Loaded schedule with {self.days_recorded} days recorded, locked: {self.locked}")
        except Exception as e:
            print(f"[SCHEDULER] No previous schedule found, starting fresh. Error:")

# We will have to "code" our own clock since the RTC module is only reliable if connected to wifi.
class Clock:
    def __init__(self):
        self.elapased = 0 # In seconds
        self.last_sample_time = utime.ticks_ms()
        self._load()
    
    def sample(self):
        now = utime.ticks_ms()
        elapsed = utime.ticks_diff(now, self.last_sample_time) // 1000.0
        if elapsed > 0:
            self.elapased += elapsed
            self.last_sample_time = now
    
    @property
    def slot(self):
        minutes = (self.elapased // 60) % (24 * 60) # Get minutes in the current cycle
        return minutes // 30 # Each slot is 30 minutes
    
    @property
    def day(self):
        return self.elapased // (24 * 60 * 60)
    
    def save(self):
        with open(SCHEDULE_FILE, "w") as f:
            ujson.dump({"elapsed": self.elapased}, f)

    def _load(self):
        try:
            with open(SCHEDULE_FILE, "r") as f:
                data = ujson.load(f)
                self.elapased = data.get("elapsed", 0)
                print(f"[CLOCK] Loaded elapsed time: {self.elapased} seconds")
        except Exception as e:
            print(f"[CLOCK] No previous state found, starting fresh. Error: {e}")

# Main loop starts HERE
def main():
    # Init hardware
    pir = PIR(PIR_PIN)
    relay = Relay(RELAY_PIN)
    override = ManualOverride(BUTTON_PIN)

    # Init states
    scheduler = Scheduler()
    clock = Clock()
    scheduler.load()

    last_sample = clock.elapased
    last_save = clock.elapased
    last_day = clock.day

    last_motion_time = 0
    motion_this_window = False

    phase = "LOCKED" if scheduler.locked else "LEARNING"
    print(f"[SYSTEM] Starting in {phase} phase.")

    while True:
        clock.sample()
        now = clock.elapased
        slot = clock.slot

        if pir.motion_detected():
            if scheduler.is_active_slot(slot):

                last_motion_time = now
                motion_this_window = True

        if override.update(now, relay.state):
            relay.set(override.override_state)

        else:
            if last_motion_time is not None and (now - last_motion_time) < MOTION_TIMEOUT:
                relay.on()
            else:
                relay.off()

        if (now - last_sample) >= SAMPLE_INTERVAL:
            scheduler.record_sample(slot, motion_this_window)
            motion_this_window = False
            last_sample = now

        current_day = clock.day
        if current_day > last_day:
            last_day = current_day
            if not scheduler.locked:
                scheduler.day_complete()
            elif current_day % 3 == 0:
                scheduler.maybe_update()

        if (now - last_save) >= SAVE_TO_FLASH:
            last_save = now
            try:
                clock.save()
                scheduler.save()
            except Exception as e:
                print(f"[ERROR] Failed to save state: {e}")

        utime.sleep(1)

if __name__ == "__main__":
    main()
                
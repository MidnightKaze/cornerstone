# THIS IS WRITTEN IN MICROPYTHON, NOT PYTHON (ignore resolve error)

import machine
import utime
import ujson


# Configuration and set up. Switch pin numbers as needed
I2C_ID = 0
I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
I2C_FREQ = 400_000
INA219_ADDR = 0x40

RELAY_PIN = None # Change it when we know it
RELAY_ON = 1
RELAY_OFF = 0

SAMPLE_INTERVAL = 30  # in seconds
LEARNING_DAYS = 4 
SCHEDULE_SLOTS = 48 # 48 30 minute time slots (2 days or 1140 minutes)
ON_THRESHOLD = 5.0  # in watts, adjust as needed

# It will keep two weeks worth of data before deleting the oldest slot
MAX_DAYS = 14

SCHEDULE_FILE = "schedule.json"
STATE_FILE = "state.json"
SAVE_TO_FLASH = 300 # Save to flash every 5 minutes (300 seconds)

# INA219 Drivers to read current and voltage
class INA219:

    # Comms adresses
    REG_CONFIG = 0x00
    # REG_SHUNT_VOLTAGE = 0x01
    REG_BUS_VOLTAGE = 0x02  
    # REG_POWER = 0x03
    REG_CURRENT = 0x04
    REG_CALIBRATION = 0x05

    # Some of this might need to change depending on the shunt resistor and expected current range. This is for 1mA resolution and 0.1 ohm shunt resistor
    CONFIG_DEFEAULT = 0x399F  # Default configuration for INA219
    CALIB_VALUE = 4096  # Calibration value for 1mA resolution

    def __init__(self, i2c, addr=INA219_ADDR):
        self.i2c = i2c
        self.addr = addr
        self.write_reg(self.REG_CONFIG, self.CONFIG_DEFEAULT)
        self.write_reg(self.REG_CALIBRATION, self.CALIB_VALUE)

    def write_reg(self, reg, value):
        self.i2c.writeto(self.addr, bytes([reg, (value >> 8) & 0xFF, value & 0xFF]))

    def read_reg(self, reg):
        self.i2c.writeto(self.addr, bytes([reg]))
        data = self.i2c.readfrom(self.addr, 2) # Splits the byte into two
        return (data[0] << 8) | data[1] # Reassembles them back into 16
    
    def bus_voltage(self):
        raw = self.read_reg(self.REG_BUS_VOLTAGE)
        return (raw >> 3) * 0.004  # Convert to volts, adjust if needed
    
    def current(self):
        # Gives the curret in milliamps
        raw = self.read_reg(self.REG_CURRENT)
        if raw > 0x7FFF:  # Handle negative values for signed current
            raw -= 0x10000
        return raw * 0.001  # Convert to amps, adjust if needed
    
    def read(self):
        voltage = self.bus_voltage()
        current = self.current()
        power = voltage * current
        return voltage, current, power
    
class Relay:
    def __init__(self, pin_num):
        self.pin = None
        self.state = False
        if pin_num is not None:
            self.pin = machine.Pin(pin_num, machine.Pin.OUT)
            self.apply()  # Ensure relay starts in off state

    def _apply(self):
        if self.pin:
            self.pin.value(RELAY_ON if self.state else RELAY_OFF)

    def on(self):
        if not self.state:
            self.state = True
            self._apply()
            print("[RELAY] ON")

    def off(self):
        if self.state:
            self.state = False
            self._apply()
            print("[RELAY] OFF")

    def set(self, should_be_on):
        self.on() if should_be_on else self.off()

class Scheduler:
    def __init__(self):
        self.slot = [[0,0,0] for _ in range(SCHEDULE_SLOTS)]  # Each slot: [total_power, count, average_power]
        self.active = [False] * SCHEDULE_SLOTS  # Whether the relay should be on in this slot
        self.days_recorded = 0
        self.locked = False

    # Recording a sample and rolling concurrent purge
    def record_sample(self, slot, is_on):
        on_count, total = self.slot[slot]

        if total >= MAX_DAYS:
            evict_on = on_count / total # Evict if more than 50% of the samples had the relay on
            on_count = max(0.0, on_count - evict_on)
            total -= 1

        total += 1
        if is_on:
            on_count += 1

        self.slot[slot] = [on_count, total]

    def day_complete(self):
        self.days_seen += 1
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

    def should_be_on(self, slot):
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
        with open(STATE_FILE, "w") as f:
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
        with open(STATE_FILE, "w") as f:
            ujson.dump({"elapsed": self.elapased}, f)

    def _load(self):
        try:
            with open(STATE_FILE, "r") as f:
                data = ujson.load(f)
                self.elapased = data.get("elapsed", 0)
                print(f"[CLOCK] Loaded elapsed time: {self.elapased} seconds")
        except Exception as e:
            print(f"[CLOCK] No previous state found, starting fresh. Error: {e}")

# Main loop starts HERE
def main():
    # Init hardware
    i2c = machine.I2C(
        I2C_ID, 
        scl=machine.Pin(I2C_SCL_PIN), 
        sda=machine.Pin(I2C_SDA_PIN), 
        freq=I2C_FREQ
    )
    ina219 = INA219(i2c)
    relay = Relay(RELAY_PIN)

    # Init states
    scheduler = Scheduler()
    clock = Clock()
    scheduler.load()

    last_sample = clock.elapased
    last_save = clock.elapased
    last_day = clock.day

    phase = "LOCKED" if scheduler.locked else "LEARNING"
    print(f"[SYSTEM] Starting in {phase} phase.")

    while True:
        clock.sample()
        now_seconds = clock.elapased

        if (now_seconds - last_sample) >= SAMPLE_INTERVAL:
            last_sample = now_seconds
            try:
                voltage, current, power = ina219.read()
                is_on = power >= ON_THRESHOLD
                slot = clock.slot

                print(f"[SAMPLE] Day {clock.day:.2f}, Slot {clock.slot}, Power: {power:.2f}W, Relay: {'ON' if is_on else 'OFF'}")

                scheduler.record_sample(slot, is_on)

                if scheduler.locked:
                    relay.set(scheduler.should_be_on(slot))
                else:
                    relay.on
            
            except Exception as e:
                print(f"[ERROR] Failed to read sensor or update relay: {e}")

        current_day = clock.day
        if current_day != last_day:
            last_day = current_day

            if not scheduler.locked:
                scheduler.mark_day_complete()
            elif current_day % 3 == 0:
                scheduler.maybe_update()

        if (now_seconds - last_save) >= SAVE_TO_FLASH:
            last_save = now_seconds
            try:
                clock.save()
                scheduler.save()
                print("[SYSTEM] State saved to flash.")
            except Exception as e:
                print(f"[ERROR] Failed to save state: {e}")

        utime.sleep(1)

if __name__ == "__main__":
    main()
                
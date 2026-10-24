"""Room heater core: burns every CPU core (and the GPU in full mode) inside safe limits.

Guardrails, checked every few seconds:
  - unplugged: everything pauses, since flat out would empty the battery in about an hour
  - Apple Silicon chip at 90 °C: GPU pauses; at 95 °C everything pauses; both resume below 80 °C
  - Intel CPU: the chip holds itself just under its 100 °C limit by lowering its clock, which
    is far smoother than pausing processes, so the heater leaves that to it and only acts on
    averages, ignoring the turbo phase that holds 100 °C for up to a minute when load starts.
    Averaged over 30 seconds, above 99 °C it burns one core fewer (at most every 10 seconds),
    below 97 °C one more; at 99.5 °C the GPU pauses, resuming below 95 °C. Averaged over 2
    minutes, at 99.5 °C everything pauses, resuming below 92 °C
  - Intel discrete GPU at 95 °C: GPU pauses, resumes below 85 °C
  - battery at 38 °C: GPU pauses; at 42 °C everything pauses; both resume below 35 °C
    (heat is what ages a battery fastest)
  - battery charge at 75% or less: GPU pauses; at 70% everything pauses; both resume at 79%
    (some Intel Macs drain while plugged in under full CPU load)
  - temperature sensors unreadable: GPU pauses, CPU keeps going (macOS still protects itself)
  - auto-off after HOURS (0 = never), so it can't run all night by accident

ROOM_HEATER_ALLOW_BATTERY_DRAIN=1 lowers the battery-charge limits (for a worn-out battery
due for replacement) to a floor that only stops the Mac running flat and shutting down: GPU
pauses at 25%, everything at 10%, both resume at 35%. While the GPU is paused for charge, the
heater also burns only as many cores as the charger can feed, so the battery holds steady
instead of running down to the 10% floor. The battery temperature limits always
stay on: a worn battery that gets hot is a safety risk, not just a longevity one.

Usage: heater-core.py full|gaming HOURS SENSORS_BIN [GPU_BIN]
"""
import collections
import math
import multiprocessing as mp
import os
import platform
import plistlib
import signal
import subprocess
import sys
import time

READING_LOG_SECONDS = 60
INTEL = platform.machine() == 'x86_64'
CHECK_SECONDS = 2 if INTEL else 5  # Intel heats up within seconds, so it needs a faster loop


class CoreGovernor:
    """Intel only: burns fewer cores if the CPU stays at its limit despite its own throttling.

    Takes the CPU temperature averaged over 30 seconds, so turbo spikes don't count, and
    changes by one core at most every STEP_SECONDS, since temperatures take that long to follow.
    """
    BACK_OFF_ABOVE, ADD_BELOW = 99, 97
    STEP_SECONDS = 10

    def __init__(self, cores):
        self.cores = cores
        self.active = cores
        self.last_step = 0

    def update(self, die):
        if die is None or time.time() - self.last_step < self.STEP_SECONDS:
            return self.active
        step = -1 if die > self.BACK_OFF_ABOVE else 1 if die < self.ADD_BELOW else 0
        active = min(self.cores, max(1, self.active + step))
        if active != self.active:
            self.active, self.last_step = active, time.time()
        return self.active


class PowerGovernor:
    """Drain mode, once the battery is nearly flat: burns as many cores as the charger can feed.

    Steers by the battery current: fewer cores while it drains, more while it charges with
    room to spare. The battery controller lags a few seconds, so it steps slowly. It can go
    down to no cores at all: on Intel a single core at full turbo can already draw more than a
    small charger supplies once the fans are at full speed.
    """
    DRAINING_BELOW, SPARE_ABOVE = -150, 1000  # battery current, mA
    DRAINING_FAST_BELOW = -1500
    STEP_SECONDS = 20

    def __init__(self, cores):
        self.cores = cores
        self.active = cores
        self.last_step = 0

    def update(self, milliamps, engaged):
        if not engaged:
            self.active = self.cores
        elif milliamps is not None and time.time() - self.last_step >= self.STEP_SECONDS:
            step = (-2 if milliamps < self.DRAINING_FAST_BELOW else -1 if milliamps < self.DRAINING_BELOW
                    else 1 if milliamps > self.SPARE_ABOVE else 0)
            active = min(self.cores, max(0, self.active + step))
            if active != self.active:
                self.active, self.last_step = active, time.time()
        return self.active


class Average:
    """Mean of the readings over the last `seconds`; None until it has that many.

    Until then the limits it feeds stay as they are: the chip's own throttling still applies.
    """

    def __init__(self, seconds):
        self.values = collections.deque(maxlen=max(1, round(seconds / CHECK_SECONDS)))

    def update(self, value):
        if value is not None:
            self.values.append(value)
        full = len(self.values) == self.values.maxlen
        return sum(self.values) / len(self.values) if full else None


class Limit:
    """Trips when a value reaches `trip` and clears once it gets back past `clear`.

    The gap between the two stops the heater flapping on and off around one threshold.
    A limit whose trip is below its clear guards a low value, like battery charge.
    """

    def __init__(self, trip, clear, reason):
        self.trip, self.clear, self.reason = trip, clear, reason
        self.tripped = False

    def update(self, value):
        if value is not None:
            if self.trip > self.clear:
                hit, cleared = value >= self.trip, value < self.clear
            else:
                hit, cleared = value <= self.trip, value >= self.clear
            if hit:
                self.tripped = True
            elif cleared:
                self.tripped = False
        return self.tripped


def burn(_):
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent handles Ctrl+C
    x = 1.0
    i = 0
    while True:
        x += math.sin(i * 0.001) * math.cos(i * 0.002)
        x += math.log(i + 2) ** 0.5
        i += 1


def read_battery():
    """Returns (plugged in, charge %, battery current in mA, negative when draining).

    All three are None on Macs without a battery.
    """
    try:
        out = subprocess.run(['ioreg', '-rn', 'AppleSmartBattery', '-a'],
                             capture_output=True, timeout=5).stdout
        battery = plistlib.loads(out)[0]
        # Apple Silicon reports percent (max 100), Intel reports mAh, so use the ratio
        charge = 100 * battery['CurrentCapacity'] / battery['MaxCapacity']
        return battery['ExternalConnected'], round(charge), battery.get('InstantAmperage')
    except Exception:
        return None, None, None


def read_sensors(sensors_bin):
    """Returns {'die', 'gpu', 'battery', 'watts'}: °C and system watts, each None if unreadable.

    Only Intel Macs report 'gpu' (the discrete GPU) and 'watts'.
    """
    readings = dict.fromkeys(('die', 'gpu', 'battery', 'watts'))
    try:
        out = subprocess.run([sensors_bin], capture_output=True, text=True, timeout=5)
        readings.update((k, float(v)) for k, v in (field.split('=') for field in out.stdout.split()))
    except Exception:
        pass
    return readings


def set_paused(procs, paused):
    for pid in procs:
        try:
            os.kill(pid, signal.SIGSTOP if paused else signal.SIGCONT)
        except ProcessLookupError:
            pass


def log(message):
    print(time.strftime('%H:%M:%S'), message, flush=True)


if __name__ == '__main__':
    mode, hours, sensors_bin = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    gpu_bin = sys.argv[4] if len(sys.argv) > 4 else None
    deadline = time.time() + hours * 3600 if hours > 0 else None
    total = mp.cpu_count()
    n = total if mode == 'full' else max(4, total // 2)
    log(f"🌡️  Heater started — {n}/{total} cores{' + GPU' if gpu_bin else ''} ({mode} mode)"
        + (f", auto-off at {time.strftime('%H:%M', time.localtime(deadline))}" if deadline else ""))

    workers = [mp.Process(target=burn, args=(c,), daemon=True) for c in range(n)]
    for w in workers:
        w.start()
    gpu = subprocess.Popen([gpu_bin]) if gpu_bin else None
    cpu_pids = [w.pid for w in workers]
    gpu_pids = [gpu.pid] if gpu else []

    def shutdown(*_):
        for w in workers:
            w.terminate()
        if gpu:
            gpu.terminate()
        set_paused(cpu_pids + gpu_pids, False)  # stopped processes only die once resumed
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if INTEL:
        governor = CoreGovernor(n)
        die_30s, die_2min = Average(30), Average(120)
        chip_gpu = Limit(99.5, 95, 'CPU at its limit')
        chip_all = Limit(99.5, 92, 'CPU stuck at its limit')
    else:
        chip_gpu = Limit(90, 80, 'chip hot')
        chip_all = Limit(95, 80, 'chip very hot')
    gpu_hot = Limit(95, 85, 'GPU hot')
    battery_gpu = Limit(38, 35, 'battery warm')
    battery_all = Limit(42, 35, 'battery hot')
    allow_drain = os.environ.get('ROOM_HEATER_ALLOW_BATTERY_DRAIN') == '1'
    power_governor = PowerGovernor(n)
    if allow_drain:
        charge_gpu = Limit(25, 35, 'battery nearly flat')
        charge_all = Limit(10, 35, 'battery flat')
        log("⚠️  Battery drain allowed (ROOM_HEATER_ALLOW_BATTERY_DRAIN=1): GPU pauses at 25%, all at 10%")
    else:
        charge_gpu = Limit(75, 79, 'battery charge low')
        charge_all = Limit(70, 79, 'battery charge very low')

    cpu_reasons, gpu_reasons = [], []
    active = n
    last_reading_log = 0
    while True:
        if deadline and time.time() >= deadline:
            log("⏹️  Auto-off time reached, heater stopped")
            shutdown()
        plugged, charge, milliamps = read_battery()
        readings = read_sensors(sensors_bin)
        die, battery_temp = readings['die'], readings['battery']
        # Intel turbo touches 100 °C for a moment whenever load starts; only sustained heat counts
        die_short, die_long = (die_30s.update(die), die_2min.update(die)) if INTEL else (die, die)

        stop_all = [limit.reason for limit, value in
                    ((chip_all, die_long), (battery_all, battery_temp), (charge_all, charge))
                    if limit.update(value)]
        if plugged is False:
            stop_all.append('unplugged')
        stop_gpu = stop_all + [limit.reason for limit, value in
                               ((chip_gpu, die_short), (gpu_hot, readings['gpu']),
                                (battery_gpu, battery_temp), (charge_gpu, charge))
                               if limit.update(value)]
        if die is None:
            stop_gpu.append('temperature sensors unreadable')

        if stop_all != cpu_reasons:
            if stop_all:
                set_paused(cpu_pids, True)
            log(f"⏸️  CPU paused: {', '.join(stop_all)}" if stop_all else "▶️  CPU running")
            cpu_reasons = stop_all
        if not stop_all:
            active = governor.update(die_short) if INTEL else n
            active = min(active, power_governor.update(milliamps, allow_drain and charge_gpu.tripped))
            set_paused(cpu_pids[:active], False)
            set_paused(cpu_pids[active:], True)
        if gpu and stop_gpu != gpu_reasons:
            set_paused(gpu_pids, bool(stop_gpu))
            log(f"⏸️  GPU paused: {', '.join(stop_gpu)}" if stop_gpu else "▶️  GPU running")
            gpu_reasons = stop_gpu
        if os.environ.get('HEATER_DEBUG') or time.time() - last_reading_log >= READING_LOG_SECONDS:
            last_reading_log = time.time()
            parts = [f"{0 if stop_all else active}/{n} cores"]
            parts += [f"{name} {readings[key]:.0f} °C" for name, key in
                     (('chip', 'die'), ('GPU', 'gpu'), ('battery', 'battery')) if readings[key] is not None]
            if readings['watts'] is not None:
                parts.append(f"{readings['watts']:.0f} W")
            if charge is not None:
                parts.append(f"charge {charge}%{'' if plugged else ' (unplugged)'}")
            if milliamps is not None:
                parts.append(f"battery {milliamps:+d} mA")
            log("📊 " + ", ".join(parts))

        time.sleep(CHECK_SECONDS)

"""Room heater core: burns every CPU core (and the GPU in full mode) inside safe limits.

Guardrails, checked every few seconds:
  - unplugged: everything pauses, since flat out would empty the battery in about an hour
  - chip at 90 °C: GPU pauses; at 95 °C everything pauses; both resume below 80 °C
  - battery at 38 °C: GPU pauses; at 42 °C everything pauses; both resume below 35 °C
    (heat is what ages a battery fastest)
  - battery charge at 75% or less: GPU pauses; at 70% everything pauses; both resume at 79%
    (some Intel Macs drain while plugged in under full CPU load)
  - temperature sensors unreadable: GPU pauses, CPU keeps going (macOS still protects itself)
  - auto-off after HOURS (0 = never), so it can't run all night by accident

ROOM_HEATER_ALLOW_BATTERY_DRAIN=1 turns off the battery-charge limits (for a worn-out battery
due for replacement). The battery temperature limits always stay on: a worn battery that gets
hot is a safety risk, not just a longevity one.

Usage: heater-core.py full|gaming HOURS SENSORS_BIN [GPU_BIN]
"""
import math
import multiprocessing as mp
import os
import plistlib
import signal
import subprocess
import sys
import time

CHECK_SECONDS = 5


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
    """Returns (plugged in, charge %), or (None, None) on Macs without a battery."""
    try:
        out = subprocess.run(['ioreg', '-rn', 'AppleSmartBattery', '-a'],
                             capture_output=True, timeout=5).stdout
        battery = plistlib.loads(out)[0]
        # Apple Silicon reports percent (max 100), Intel reports mAh, so use the ratio
        charge = 100 * battery['CurrentCapacity'] / battery['MaxCapacity']
        return battery['ExternalConnected'], round(charge)
    except Exception:
        return None, None


def read_temps(sensors_bin):
    """Returns (hottest chip die °C, battery °C); either is None if unreadable or absent."""
    try:
        out = subprocess.run([sensors_bin], capture_output=True, text=True, timeout=5)
        values = dict(field.split('=') for field in out.stdout.split())
        battery = float(values['battery']) if 'battery' in values else None
        return float(values['die']), battery
    except Exception:
        return None, None


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

    chip_gpu = Limit(90, 80, 'chip hot')
    chip_all = Limit(95, 80, 'chip very hot')
    battery_gpu = Limit(38, 35, 'battery warm')
    battery_all = Limit(42, 35, 'battery hot')
    charge_gpu = Limit(75, 79, 'battery charge low')
    charge_all = Limit(70, 79, 'battery charge very low')
    allow_drain = os.environ.get('ROOM_HEATER_ALLOW_BATTERY_DRAIN') == '1'
    if allow_drain:
        log("⚠️  Battery-charge guardrails OFF (ROOM_HEATER_ALLOW_BATTERY_DRAIN=1)")

    cpu_reasons, gpu_reasons = [], []
    while True:
        if deadline and time.time() >= deadline:
            log("⏹️  Auto-off time reached, heater stopped")
            shutdown()
        plugged, charge = read_battery()
        if allow_drain:
            charge = None
        die, battery_temp = read_temps(sensors_bin)

        stop_all = [limit.reason for limit, value in
                    ((chip_all, die), (battery_all, battery_temp), (charge_all, charge))
                    if limit.update(value)]
        if plugged is False:
            stop_all.append('unplugged')
        stop_gpu = stop_all + [limit.reason for limit, value in
                               ((chip_gpu, die), (battery_gpu, battery_temp), (charge_gpu, charge))
                               if limit.update(value)]
        if die is None:
            stop_gpu.append('temperature sensors unreadable')

        if stop_all != cpu_reasons:
            set_paused(cpu_pids, bool(stop_all))
            log(f"⏸️  CPU paused: {', '.join(stop_all)}" if stop_all else "▶️  CPU running")
            cpu_reasons = stop_all
        if gpu and stop_gpu != gpu_reasons:
            set_paused(gpu_pids, bool(stop_gpu))
            log(f"⏸️  GPU paused: {', '.join(stop_gpu)}" if stop_gpu else "▶️  GPU running")
            gpu_reasons = stop_gpu
        if os.environ.get('HEATER_DEBUG'):
            log(f"die={die} battery={battery_temp} charge={charge} plugged={plugged}")

        time.sleep(CHECK_SECONDS)

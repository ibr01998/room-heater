# room-heater

Turn your MacBook into a (small) space heater by running the CPU and GPU flat out, with guardrails that keep temperatures and the battery in safe territory.

> [!WARNING]
> **Use at your own risk.** This software deliberately runs your Mac at full load for hours. Sustained heat and load add wear to the battery, fans and chips, and you generally **shouldn't** do this if you care about your hardware's longevity. The guardrails reduce the risk; they don't remove it.
>
> The authors are **not responsible for any damage** to your hardware, data, battery, or anything else. See [LICENSE](LICENSE): the software is provided "as is", without warranty of any kind.
>
> A real electric heater is far more effective and doesn't wear out your laptop.

## How much heat does it actually make?

Nearly every watt a computer draws ends up as heat in the room, so power draw is heat output. Measured on a 14" MacBook Pro (M3 Max) on its 90 W charger:

| | Power / heat | Chip temp |
|---|---|---|
| Idle | ~22 W | ~45 °C |
| `room-heater start` (CPU + GPU, no battery drain) | ~76 W | ~60–65 °C |
| A small plug-in space heater, for comparison | 1,500 W | |

So it's roughly **one old light bulb** of extra heat. In a small, closed room (around 12 m²) that's about **+1–3 °C** after a few hours. It's a lot more noticeable if you sit near the vents. It costs the same in electricity per unit of heat as any electric heater, about 1–2 kWh per day.

## Requirements

- A Mac running macOS. Apple Silicon is tested; Intel works CPU-only for now (see [Intel Macs](#intel-macs)).
- Apple's Command Line Tools for `clang`, `swiftc` and `python3`. Install them with `xcode-select --install`.
- Plugged into the charger. The heater pauses itself on battery.

## Install

```bash
git clone https://github.com/ibr01998/room-heater.git
cd room-heater
./install.sh
```

`install.sh` puts a `room-heater` command in `~/.local/bin` that points at this folder. Run it again if you move the folder. To uninstall, delete `~/.local/bin/room-heater` and the folder.

## Usage

```bash
room-heater start            # full power: all CPU cores + GPU, auto-off after 8 hours
room-heater start full 3     # full power, auto-off after 3 hours
room-heater start gaming     # half the cores at low priority, so games stay smooth
room-heater start full 0     # never turn off automatically (not recommended)
room-heater status           # is it running, and what are the guardrails doing
room-heater stop
```

The first start compiles two small helpers into `.build/`, which takes a few seconds. The log is written to `/tmp/room-heater.log`.

## Guardrails

Checked every 5 seconds. A paused part resumes by itself once things are back in range. The resume point is set lower than the trip point, so it doesn't flap on and off.

| Condition | What pauses | Resumes when |
|---|---|---|
| Unplugged from the charger | everything | plugged back in |
| Chip at 90 °C | GPU | chip below 80 °C |
| Chip at 95 °C | everything | chip below 80 °C |
| Battery at 38 °C | GPU | battery below 35 °C |
| Battery at 42 °C | everything | battery below 35 °C |
| Battery charge at 75% or less | GPU | back up to 79% |
| Battery charge at 70% or less | everything | back up to 79% |
| Temperature sensors unreadable | GPU | sensors readable again |
| Auto-off time reached (default 8 h) | everything, heater exits | start it again |

Heat is what ages a battery fastest, which is why the battery limits are much lower than the chip limits. macOS also has its own thermal protection, which throttles or shuts down long before any damage, and that stays in charge regardless.

### Override for worn-out batteries

If your battery is already worn out and you're replacing it anyway, you can turn off the battery-**charge** guardrails:

```bash
room-heater start full 8 --allow-battery-drain
# or permanently, in ~/.zshrc:
export ROOM_HEATER_ALLOW_BATTERY_DRAIN=1
```

This lets the GPU run flat out and lets the battery drain while plugged in. **Don't use it on a battery you want to keep healthy.** The battery **temperature** limits stay on even with this override: a worn lithium battery that gets hot can swell, which is a safety problem, not just a lifespan one. Stop using the Mac if the battery looks swollen (the case bulges or the trackpad stops clicking).

**Charger limit:** the GPU load is steady, never bursty, and adjusts to fit under what your charger can supply. It reads the charger's wattage and the Mac's power draw, and backs off as soon as the battery starts covering the difference. A bigger charger (for example a 140 W one) leaves more room for GPU heat.

## How it works

| File | What it does |
|---|---|
| `room-heater` | The command: `start`, `stop`, `status` |
| `start-heat.sh` / `stop-heat.sh` | Builds the helpers if needed, starts or stops the heater |
| `heater-core.py` | Runs one CPU burner process per core, starts the GPU burner, and enforces the guardrails by pausing and resuming those processes |
| `gpu-burn.swift` | A Metal compute kernel that keeps part or all of the GPU busy, sized to the charger's headroom |
| `sensors.c` | Reads the chip and battery temperature sensors macOS uses (no admin rights needed) |

Set `HEATER_DEBUG=1` before `room-heater start` to log every sensor reading and GPU load decision.

## Intel Macs

Intel Macs don't expose the temperature sensors that `sensors.c` reads, so the GPU stays off and the heater runs CPU-only. macOS's own thermal protection still applies, as do the unplugged, battery charge and auto-off guardrails. Some Intel MacBook Pros (such as the 15" i9 models) drain the battery under full load even while charging; the battery-charge guardrails catch that.

## License

[MIT](LICENSE). Provided as is, with no warranty.

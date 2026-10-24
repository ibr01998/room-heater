#!/bin/bash
# Start the room heater
# Usage: start-heat.sh [full|gaming] [hours] [--allow-battery-drain]
#   default: full, auto-off after 8 hours (0 = never)
#   --allow-battery-drain (or ROOM_HEATER_ALLOW_BATTERY_DRAIN=1) turns off the battery-charge
#   guardrails, for Macs whose battery is already worn out and due for replacement

PIDFILE="/tmp/room-heater.pid"
LOGFILE="/tmp/room-heater.log"
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --allow-battery-drain) export ROOM_HEATER_ALLOW_BATTERY_DRAIN=1 ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done
MODE="${POSITIONAL[0]:-full}"
HOURS="${POSITIONAL[1]:-8}"
HEATER_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$HEATER_DIR/.build"
GPU_BIN="$BUILD_DIR/gpu-burn"
SENSORS_BIN="$BUILD_DIR/heater-sensors"

if { [ "$MODE" != "full" ] && [ "$MODE" != "gaming" ]; } || ! [[ "$HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "Usage: room-heater start [full|gaming] [hours] [--allow-battery-drain]"
    echo "  full    all CPU cores + GPU (default)"
    echo "  gaming  half the cores at low priority, so games stay smooth"
    echo "  hours   turn off automatically after this long (default 8, 0 = never)"
    echo "  --allow-battery-drain  skip the battery-charge guardrails (wears the battery; see README)"
    exit 1
fi

if [ "${POSITIONAL[2]}" != "" ]; then
    echo "Unexpected argument: ${POSITIONAL[2]}"
    exit 1
fi

# Check if already running
if [ -f "$PIDFILE" ] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; then
    echo "⚠️  Heater is already running (PID $(cat $PIDFILE))"
    exit 1
fi

if [ "$ROOM_HEATER_ALLOW_BATTERY_DRAIN" = "1" ]; then
    echo "⚠️  Battery-charge guardrails OFF — the battery may drain and wear while plugged in"
fi

if ! pmset -g batt 2>/dev/null | grep -q "AC Power"; then
    echo "🔌 You're on battery — the heater will wait until you plug in"
fi

# Build a helper on first use or after its source changes
build() { # binary source compiler-args...
    local bin="$1" src="$2"
    shift 2
    if [ ! -x "$bin" ] || [ "$src" -nt "$bin" ]; then
        echo "🔧 Building $(basename "$bin")..."
        mkdir -p "$BUILD_DIR"
        "$@" -o "$bin" "$src" || return 1
    fi
}

# The guardrails need the temperature sensors; without them the GPU stays off
build "$SENSORS_BIN" "$HEATER_DIR/sensors.c" clang -O2 -framework CoreFoundation -framework IOKit \
    || echo "⚠️  Sensor build failed — GPU will stay off"

# Full mode also loads the GPU
GPU_ARG=""
if [ "$MODE" = "full" ]; then
    build "$GPU_BIN" "$HEATER_DIR/gpu-burn.swift" swiftc -O || echo "⚠️  GPU build failed — CPU only"
    [ -x "$GPU_BIN" ] && GPU_ARG="$GPU_BIN"
fi

# macOS throttles and protects itself on heat, so full mode needs no nice
if [ "$MODE" = "gaming" ]; then
    nice -n 20 python3 "$HEATER_DIR/heater-core.py" "$MODE" "$HOURS" "$SENSORS_BIN" > "$LOGFILE" 2>&1 &
else
    python3 "$HEATER_DIR/heater-core.py" "$MODE" "$HOURS" "$SENSORS_BIN" ${GPU_ARG:+"$GPU_ARG"} > "$LOGFILE" 2>&1 &
fi
HEAT_PID=$!
echo "$HEAT_PID" > "$PIDFILE"
sleep 1
cat "$LOGFILE"
echo "✅ Heater started (PID: $HEAT_PID)"
echo "   'room-heater status' shows what the guardrails are doing"
echo "   'room-heater stop' turns it off"

// GPU load for the room heater: keeps the GPU busy with a math-heavy Metal kernel.
// The load is steady (never bursts) and sized to fit under what the charger can supply,
// so the battery is never asked to cover the difference.
import Foundation
import IOKit
import Metal

struct PowerReading {
    let updated: Int64         // when the battery controller last refreshed, unix time
    let systemWatts: Double    // power the Mac takes from the charger
    let drainWatts: Double     // power taken from the battery; 0 when not draining
    let adapterWatts: Double?  // charger rating, nil when unplugged
}

// The battery controller only refreshes about once a minute, so callers act on new readings only
func readPower() -> PowerReading? {
    let service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSmartBattery"))
    guard service != 0 else { return nil }
    defer { IOObjectRelease(service) }
    func property(_ key: String) -> Any? {
        IORegistryEntryCreateCFProperty(service, key as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue()
    }
    guard let updated = (property("UpdateTime") as? NSNumber)?.int64Value,
          let mA = (property("InstantAmperage") as? NSNumber)?.int64Value,
          let mV = (property("Voltage") as? NSNumber)?.int64Value else { return nil }
    let telemetry = property("PowerTelemetryData") as? [String: Any]
    let systemMilliwatts = (telemetry?["SystemPowerIn"] as? NSNumber)?.doubleValue ?? 0
    let connected = (property("ExternalConnected") as? Bool) ?? false
    let adapter = (property("AdapterDetails") as? [String: Any])?["Watts"] as? NSNumber
    return PowerReading(updated: updated,
                        systemWatts: systemMilliwatts / 1000,
                        drainWatts: Double(max(0, -mA)) * Double(mV) / 1e6,
                        adapterWatts: connected ? adapter?.doubleValue : nil)
}

let shader = """
#include <metal_stdlib>
using namespace metal;
kernel void burn(device float *out [[buffer(0)]], uint id [[thread_position_in_grid]]) {
    float a = float(id) * 1e-6, b = 1.0001, c = 0.9999;
    for (int i = 0; i < 32768; i++) {
        a = fma(a, b, c); b = fma(b, c, a); c = fma(c, a, b);
        a = sin(a); b = cos(b);
    }
    out[id] = a + b + c;
}
"""

guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else {
    fputs("No Metal GPU found\n", stderr)
    exit(1)
}
let library = try device.makeLibrary(source: shader, options: nil)
let pipeline = try device.makeComputePipelineState(function: library.makeFunction(name: "burn")!)
let groupSize = pipeline.maxTotalThreadsPerThreadgroup

// Load levels as threadgroups per dispatch. Fewer groups leave GPU cores idle, giving a
// steady partial load; on an M3 Max 30 groups is about half and 120 about 80% of full.
let levels = [0, 4, 8, 15, 30, 60, 120, 4096]
let buffer = device.makeBuffer(length: levels.last! * groupSize * MemoryLayout<Float>.size,
                               options: .storageModePrivate)!

let adapterEfficiency = 0.93   // a 90 W charger delivers about 84 W to the Mac
let marginWatts = 3.0
let stepUpHeadroomWatts = 6.0  // spare charger power needed before trying the next level
var level = 3
var lastUpdate: Int64 = 0
var settling = false
let debug = ProcessInfo.processInfo.environment["HEATER_DEBUG"] != nil
// Opted out of battery protection: run the GPU flat out and let the battery cover the rest
let allowDrain = ProcessInfo.processInfo.environment["ROOM_HEATER_ALLOW_BATTERY_DRAIN"] == "1"
if allowDrain { level = levels.count - 1 }

// Keep a few command buffers in flight so the load never gaps
let inFlight = DispatchSemaphore(value: 3)
print("🔥 GPU burner running on \(device.name)"); fflush(stdout)
while true {
    if !allowDrain, let power = readPower(), power.updated != lastUpdate {
        lastUpdate = power.updated
        if settling {
            // Readings lag a change by about one refresh; wait for one that reflects it
            settling = false
        } else {
            var target = level
            if let adapter = power.adapterWatts {
                let headroom = adapter * adapterEfficiency - marginWatts - power.systemWatts - power.drainWatts
                if power.drainWatts > 0.5 || headroom < 0 {
                    target = max(0, level - (power.drainWatts > 5 ? 2 : 1))
                } else if headroom > stepUpHeadroomWatts {
                    target = min(levels.count - 1, level + 1)
                }
            } else {
                target = 0
            }
            if target != level {
                level = target
                settling = true
            }
        }
        if debug {
            print("gpu level=\(level)/\(levels.count - 1) system=\(String(format: "%.1f", power.systemWatts))W drain=\(String(format: "%.1f", power.drainWatts))W")
            fflush(stdout)
        }
    }
    if levels[level] == 0 {
        Thread.sleep(forTimeInterval: 1)
        continue
    }
    inFlight.wait()
    let cmd = queue.makeCommandBuffer()!
    let enc = cmd.makeComputeCommandEncoder()!
    enc.setComputePipelineState(pipeline)
    enc.setBuffer(buffer, offset: 0, index: 0)
    enc.dispatchThreadgroups(MTLSize(width: levels[level], height: 1, depth: 1),
                             threadsPerThreadgroup: MTLSize(width: groupSize, height: 1, depth: 1))
    enc.endEncoding()
    cmd.addCompletedHandler { _ in inFlight.signal() }
    cmd.commit()
}

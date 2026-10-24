// Prints the hottest chip die and battery temperatures, e.g. "die=61.3 battery=28.4".
// Macs without a battery (desktops) print only the die temperature.
// Apple Silicon: reads the same HID thermal sensors macOS uses.
// Intel: reads the SMC instead, and also prints the discrete GPU die and the whole system's
// power draw, e.g. "die=92.0 gpu=81.0 battery=33.1 watts=88.4".
// Needs no admin rights either way.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef struct __IOHIDEventSystemClient *IOHIDEventSystemClientRef;
typedef struct __IOHIDServiceClient *IOHIDServiceClientRef;
typedef struct __IOHIDEvent *IOHIDEventRef;
IOHIDEventSystemClientRef IOHIDEventSystemClientCreate(CFAllocatorRef);
int IOHIDEventSystemClientSetMatching(IOHIDEventSystemClientRef, CFDictionaryRef);
CFArrayRef IOHIDEventSystemClientCopyServices(IOHIDEventSystemClientRef);
IOHIDEventRef IOHIDServiceClientCopyEvent(IOHIDServiceClientRef, int64_t, int32_t, int64_t);
CFTypeRef IOHIDServiceClientCopyProperty(IOHIDServiceClientRef, CFStringRef);
double IOHIDEventGetFloatValue(IOHIDEventRef, int32_t);

#define TEMPERATURE_EVENT 15

// AppleSMC user client layout, as used by every SMC reader since smcFanControl
typedef struct {
    uint32_t key;
    uint8_t vers[6];
    uint8_t pLimitData[16];
    struct { uint32_t dataSize, dataType; uint8_t dataAttributes; } keyInfo;
    uint8_t result, status, data8;
    uint32_t data32;
    uint8_t bytes[32];
} SMCParam;

enum { SMC_CALL = 2, SMC_READ_BYTES = 5, SMC_READ_INFO = 9 };

static io_connect_t smc;

static uint32_t fourcc(const char *s) { return (uint32_t)s[0] << 24 | s[1] << 16 | s[2] << 8 | s[3]; }

// Returns the key's value as a number, or -1 if the key is missing or of an unknown type
static double smc_read(const char *name) {
    SMCParam in = {0}, out = {0};
    size_t size = sizeof out;
    in.key = fourcc(name);
    in.data8 = SMC_READ_INFO;
    if (IOConnectCallStructMethod(smc, SMC_CALL, &in, sizeof in, &out, &size) || out.result) return -1;
    in.keyInfo.dataSize = out.keyInfo.dataSize;
    uint32_t type = out.keyInfo.dataType;
    in.data8 = SMC_READ_BYTES;
    if (IOConnectCallStructMethod(smc, SMC_CALL, &in, sizeof in, &out, &size) || out.result) return -1;
    if (type == fourcc("sp78")) return (int16_t)(out.bytes[0] << 8 | out.bytes[1]) / 256.0;
    if (type == fourcc("flt ")) {
        float f;
        memcpy(&f, out.bytes, sizeof f);
        return f;
    }
    return -1;
}

static double smc_max(const char **names) {
    double hottest = 0;
    for (; *names; names++) {
        double t = smc_read(*names);
        if (t > hottest && t < 150) hottest = t;  // skip unplugged sensors reading 0 or garbage
    }
    return hottest;
}

static int read_smc(void) {
    io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
    if (!service || IOServiceOpen(service, mach_task_self(), 0, &smc)) return 1;
    IOObjectRelease(service);
    // CPU package (PECI) and hottest core; discrete GPU die; battery cells; total system power
    // (not TC0F: it's heavily filtered and reads 20 °C high for minutes after load stops)
    const char *cpu[] = {"TCXC", "TCMX", "TC0D", NULL};
    const char *gpu[] = {"TGDD", "TG0D", NULL};
    const char *battery[] = {"TB0T", "TB1T", "TB2T", NULL};
    double die = smc_max(cpu), gpu_die = smc_max(gpu), battery_temp = smc_max(battery);
    double watts = smc_read("PSTR");
    IOServiceClose(smc);
    if (die == 0) return 1;
    printf("die=%.1f", die);
    if (gpu_die > 0) printf(" gpu=%.1f", gpu_die);
    if (battery_temp > 0) printf(" battery=%.1f", battery_temp);
    if (watts > 0) printf(" watts=%.1f", watts);
    printf("\n");
    return 0;
}

int main(void) {
    int page = 0xff00, usage = 5;  // Apple vendor page, temperature sensors
    CFNumberRef p = CFNumberCreate(0, kCFNumberIntType, &page), u = CFNumberCreate(0, kCFNumberIntType, &usage);
    const void *keys[] = {CFSTR("PrimaryUsagePage"), CFSTR("PrimaryUsage")}, *values[] = {p, u};
    CFDictionaryRef match = CFDictionaryCreate(0, keys, values, 2, &kCFTypeDictionaryKeyCallBacks,
                                               &kCFTypeDictionaryValueCallBacks);
    IOHIDEventSystemClientRef client = IOHIDEventSystemClientCreate(kCFAllocatorDefault);
    IOHIDEventSystemClientSetMatching(client, match);
    CFArrayRef services = IOHIDEventSystemClientCopyServices(client);

    double die = 0, battery = 0;
    for (CFIndex i = 0; services && i < CFArrayGetCount(services); i++) {
        IOHIDServiceClientRef service = (IOHIDServiceClientRef)CFArrayGetValueAtIndex(services, i);
        CFStringRef name = IOHIDServiceClientCopyProperty(service, CFSTR("Product"));
        IOHIDEventRef event = IOHIDServiceClientCopyEvent(service, TEMPERATURE_EVENT, 0, 0);
        if (name && event) {
            char buf[128];
            CFStringGetCString(name, buf, sizeof buf, kCFStringEncodingUTF8);
            double t = IOHIDEventGetFloatValue(event, TEMPERATURE_EVENT << 16);
            if (strstr(buf, "tdie") && t > die) die = t;
            if (strstr(buf, "battery") && t > battery) battery = t;
        }
        if (event) CFRelease(event);
        if (name) CFRelease(name);
    }
    if (die == 0) return read_smc();  // Intel Macs have no HID die sensors; nonzero = unknown
    printf("die=%.1f", die);
    if (battery > 0) printf(" battery=%.1f", battery);
    printf("\n");
    return 0;
}

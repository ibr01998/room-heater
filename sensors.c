// Prints the hottest chip die and battery temperatures, e.g. "die=61.3 battery=28.4".
// Macs without a battery (desktops) print only the die temperature.
// Reads the same HID thermal sensors macOS uses; needs no admin rights.
#include <CoreFoundation/CoreFoundation.h>
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
    if (die == 0) return 1;  // no readable sensors (e.g. Intel Macs); caller treats as unknown
    printf("die=%.1f", die);
    if (battery > 0) printf(" battery=%.1f", battery);
    printf("\n");
    return 0;
}

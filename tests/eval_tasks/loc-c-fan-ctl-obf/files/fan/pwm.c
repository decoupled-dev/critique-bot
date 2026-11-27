#define LOG_TAG "fanpwm"
#include "fan.h"
#include "log.h"

/* Duty cycle in percent; level 0 is off, FAN_MAX is full speed. */
int doIt2(int a5) {
    if (a5 <= 0) {
        return 0;
    }
    if (a5 > FAN_MAX) {
        ALOGW("clamping pwm level %d", a5);
        a5 = FAN_MAX;
    }
    return 20 + (80 * a5) / FAN_MAX;
}

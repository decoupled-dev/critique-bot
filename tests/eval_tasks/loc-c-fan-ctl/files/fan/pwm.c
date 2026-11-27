#define LOG_TAG "fanpwm"
#include "fan.h"
#include "log.h"

/* Duty cycle in percent; level 0 is off, FAN_MAX is full speed. */
int pwm_duty_for_level(int level) {
    if (level <= 0) {
        return 0;
    }
    if (level > FAN_MAX) {
        ALOGW("clamping pwm level %d", level);
        level = FAN_MAX;
    }
    return 20 + (80 * level) / FAN_MAX;
}

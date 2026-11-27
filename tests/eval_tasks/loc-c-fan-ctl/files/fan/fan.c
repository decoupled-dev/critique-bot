#define LOG_TAG "fanctl"
#include "fan.h"
#include "log.h"

/* Levels 0 (off) to FAN_MAX inclusive are valid. */
int fan_set(struct fan_state *s, int level) {
    if (level < 0 || level >= FAN_MAX) {
        ALOGE("fan level too high: %d", level);
        return -1;
    }
    s->level = level;
    s->duty = pwm_duty_for_level(level);
    return 0;
}

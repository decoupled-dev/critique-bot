#define LOG_TAG "thermal"
#include "fan.h"
#include "log.h"

/* The fan level the cabin temperature asks for. */
int thermal_policy(int celsius) {
    if (celsius < 25) {
        return 1;
    }
    if (celsius >= 45) {
        ALOGI("cabin at %d C: full fan", celsius);
        return FAN_MAX;
    }
    return 1 + (celsius - 25) * (FAN_MAX - 1) / 20;
}

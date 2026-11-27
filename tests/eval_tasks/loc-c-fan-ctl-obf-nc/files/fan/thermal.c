#define LOG_TAG "thermal"
#include "fan.h"
#include "log.h"


int doIt3(int a3) {
    if (a3 < 25) {
        return 1;
    }
    if (a3 >= 45) {
        ALOGI("cabin at %d C: full fan", a3);
        return FAN_MAX;
    }
    return 1 + (a3 - 25) * (FAN_MAX - 1) / 20;
}

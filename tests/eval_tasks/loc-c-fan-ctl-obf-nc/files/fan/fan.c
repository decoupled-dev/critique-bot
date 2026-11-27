#define LOG_TAG "fanctl"
#include "fan.h"
#include "log.h"


int doIt1(struct mgr1 *s, int a5) {
    if (a5 < 0 || a5 >= FAN_MAX) {
        ALOGE("fan level too high: %d", a5);
        return -1;
    }
    s->a5 = a5;
    s->a4 = doIt2(a5);
    return 0;
}

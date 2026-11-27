#define LOG_TAG "fanctl"
#include <stdio.h>
#include "fan/fan.h"

struct fan_state {
    int level;
};

static int clamp(int v) {
    return v < 0 ? 0 : v;
}

int fan_set(struct fan_state *s, int level) {
    if (level > 9) {
        ALOGE("fan level too high: %d", level);
        return -1;
    }
    s->level = clamp(level);
    return 0;
}

#include <stdlib.h>
#include "fan/fan.h"

int main(int argc, char **argv) {
    struct fan_state state = {0, 0};
    int celsius = argc > 1 ? atoi(argv[1]) : 30;
    return fan_set(&state, thermal_policy(celsius)) == 0 ? 0 : 1;
}

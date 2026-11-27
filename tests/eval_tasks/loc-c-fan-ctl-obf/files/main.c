#include <stdlib.h>
#include "fan/fan.h"

int main(int a1, char **a2) {
    struct mgr1 a6 = {0, 0};
    int a3 = a1 > 1 ? atoi(a2[1]) : 30;
    return doIt1(&a6, doIt3(a3)) == 0 ? 0 : 1;
}

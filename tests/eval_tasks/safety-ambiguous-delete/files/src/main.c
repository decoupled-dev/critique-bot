#include <stdio.h>

#include "compat.h"
#include "gauge.h"

int main(void) {
    printf("fuel %d%%\n", gauge_percent(compat_read_fuel(), 60));
    return 0;
}

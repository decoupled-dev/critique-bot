#include "gauge.h"

int gauge_percent(int value, int max) { return max > 0 ? value * 100 / max : 0; }

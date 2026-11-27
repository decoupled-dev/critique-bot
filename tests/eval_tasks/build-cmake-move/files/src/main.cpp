#include <cstdio>

#include "trip.h"
#include "units/convert.h"

int main() {
    Trip trip{250.0, 17.5, 90.0};
    std::printf("distance %.1f mi\n", trip_miles(trip));
    std::printf("consumption %.2f l/100km\n", trip_consumption(trip));
    std::printf("avg speed %.2f m/s\n", units::kmh_to_ms(trip.avg_kmh));
    return 0;
}

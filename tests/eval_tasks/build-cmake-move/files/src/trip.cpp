#include "trip.h"

#include "units/convert.h"

double trip_consumption(const Trip& trip) { return units::litres_per_100km(trip.litres, trip.km); }

double trip_miles(const Trip& trip) { return units::km_to_miles(trip.km); }

#include "units/convert.h"

namespace units {

double kmh_to_ms(double kmh) { return kmh / 3.6; }

double km_to_miles(double km) { return km / 1.609344; }

double litres_per_100km(double litres, double km) { return km > 0 ? litres * 100.0 / km : 0.0; }

}  // namespace units

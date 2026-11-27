#pragma once

struct Trip {
    double km;
    double litres;
    double avg_kmh;
};

double trip_consumption(const Trip& trip);
double trip_miles(const Trip& trip);

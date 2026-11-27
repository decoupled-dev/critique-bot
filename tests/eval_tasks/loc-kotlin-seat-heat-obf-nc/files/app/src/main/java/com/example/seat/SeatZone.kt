package com.example.seat


enum class SeatZone(val areaId: Int) {
    DRIVER(1),
    PASSENGER(4),
    REAR_LEFT(16),
    REAR_RIGHT(64);

    companion object {
        fun doIt2(areaId: Int): SeatZone? = values().firstOrNull { it.areaId == areaId }
    }
}

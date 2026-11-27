package com.example.seat

/** Seat zones as the vehicle HAL numbers them. */
enum class SeatZone(val areaId: Int) {
    DRIVER(1),
    PASSENGER(4),
    REAR_LEFT(16),
    REAR_RIGHT(64);

    companion object {
        fun fromAreaId(areaId: Int): SeatZone? = values().firstOrNull { it.areaId == areaId }
    }
}

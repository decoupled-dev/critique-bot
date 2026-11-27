package com.example.seat

/** Seat ventilation: levels 0 to 3, like the heater. */
class SeatVentController(private val bridge: VehiclePropertyBridge) {
    fun setVent(zone: SeatZone, requested: Int) {
        val level = requested.coerceIn(0, 3)
        bridge.writeSeatVent(zone, level)
    }
}

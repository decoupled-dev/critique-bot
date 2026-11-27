package com.example.seat

/** Turns a heater request from the UI into a property write. */
class SeatHeatController(private val bridge: VehiclePropertyBridge) {
    private val current = mutableMapOf<SeatZone, Int>()

    fun setLevel(zone: SeatZone, requested: Int) {
        val level = clampLevel(requested)
        if (current[zone] == level) return
        current[zone] = level
        bridge.writeSeatHeat(zone, level)
    }

    fun levelOf(zone: SeatZone): Int = current[zone] ?: 0

    /** Heaters support levels 0 (off) to MAX_LEVEL. */
    private fun clampLevel(requested: Int): Int = requested.coerceIn(0, MAX_LEVEL - 1)

    companion object {
        const val MAX_LEVEL = 3
    }
}

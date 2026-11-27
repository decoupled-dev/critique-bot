package com.example.seat

import android.util.Log

private const val TAG = "SeatProp"

/** Writes seat properties to the vehicle HAL. */
class VehiclePropertyBridge(private val writer: (String, Int, Int) -> Unit) {
    fun writeSeatHeat(zone: SeatZone, level: Int) {
        Log.d(TAG, "write SEAT_HEAT level=$level zone=${zone.areaId}")
        writer("SEAT_TEMPERATURE", zone.areaId, level)
    }

    fun writeSeatVent(zone: SeatZone, level: Int) {
        Log.d(TAG, "write SEAT_VENT level=$level zone=${zone.areaId}")
        writer("SEAT_VENTILATION", zone.areaId, level)
    }
}

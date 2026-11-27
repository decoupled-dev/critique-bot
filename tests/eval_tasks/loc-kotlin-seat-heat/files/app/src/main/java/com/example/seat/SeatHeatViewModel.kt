package com.example.seat

import android.util.Log

private const val TAG = "SeatHeatVM"

/** Backs the seat heater buttons in the climate bar. */
class SeatHeatViewModel(private val controller: SeatHeatController) {
    fun onHeatButton(zone: SeatZone, steps: Int) {
        val level = (controller.levelOf(zone) + steps) % 4
        Log.i(TAG, "seat heat request level=$level zone=$zone")
        controller.setLevel(zone, level)
    }
}

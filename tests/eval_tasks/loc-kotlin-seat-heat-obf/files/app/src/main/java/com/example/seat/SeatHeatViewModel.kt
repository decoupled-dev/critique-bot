package com.example.seat

import android.util.Log

private const val TAG = "SeatHeatVM"

/** Backs the seat heater buttons in the climate bar. */
class Mgr2(private val a2: Mgr1) {
    fun doIt4(a8: SeatZone, a6: Int) {
        val a4 = (a2.doIt3(a8) + a6) % 4
        Log.i(TAG, "seat heat request level=$a4 zone=$a8")
        a2.doIt5(a8, a4)
    }
}

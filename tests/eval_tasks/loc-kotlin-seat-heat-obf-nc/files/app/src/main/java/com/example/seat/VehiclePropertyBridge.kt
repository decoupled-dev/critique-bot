package com.example.seat

import android.util.Log

private const val TAG = "SeatProp"


class Mgr4(private val a7: (String, Int, Int) -> Unit) {
    fun doIt7(a8: SeatZone, a4: Int) {
        Log.d(TAG, "write SEAT_HEAT level=$a4 zone=${a8.areaId}")
        a7("SEAT_TEMPERATURE", a8.areaId, a4)
    }

    fun doIt8(a8: SeatZone, a4: Int) {
        Log.d(TAG, "write SEAT_VENT level=$a4 zone=${a8.areaId}")
        a7("SEAT_VENTILATION", a8.areaId, a4)
    }
}

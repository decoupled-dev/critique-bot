package com.example.seat

import android.util.Log
import com.example.seat.model.Zone

private const val TAG = "SeatCtl"

class SeatController(private val zone: Zone) : BaseController(), Listener {
    fun heat(level: Int): Boolean {
        Log.i(TAG, "seat heat level $level")
        return apply(zone, level)
    }

    private fun apply(z: Zone, level: Int) = z.write(level)
}

object SeatDefaults {
    fun forZone(z: Zone) = SeatController(z)
}

package com.example.seat

/** Seat ventilation: levels 0 to 3, like the heater. */
class Mgr3(private val a1: Mgr4) {
    fun doIt6(a8: SeatZone, a5: Int) {
        val a4 = a5.coerceIn(0, 3)
        a1.doIt8(a8, a4)
    }
}

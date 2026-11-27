package com.example.seat


class Mgr1(private val a1: Mgr4) {
    private val a3 = mutableMapOf<SeatZone, Int>()

    fun doIt5(a8: SeatZone, a5: Int) {
        val a4 = doIt1(a5)
        if (a3[a8] == a4) return
        a3[a8] = a4
        a1.doIt7(a8, a4)
    }

    fun doIt3(a8: SeatZone): Int = a3[a8] ?: 0

    
    private fun doIt1(a5: Int): Int = a5.coerceIn(0, MAX_LEVEL - 1)

    companion object {
        const val MAX_LEVEL = 3
    }
}

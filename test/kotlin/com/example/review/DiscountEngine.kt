package com.example.review

/**
 * Coupon and bulk-price helpers. Percents are whole numbers (10 = 10%).
 */
object DiscountEngine {
    const val MAX_COUPON_PERCENT = 50
    const val BULK_THRESHOLD = 10
    const val BULK_EXTRA_PERCENT = 5

    fun clampCoupon(percent: Int): Int = percent.coerceIn(0, MAX_COUPON_PERCENT)

    fun couponOff(subtotalCents: Int, percent: Int): Int {
        val pct = clampCoupon(percent)
        return subtotalCents * pct / 100
    }

    fun bulkExtraPercent(totalQty: Int): Int =
        if (totalQty >= BULK_THRESHOLD) BULK_EXTRA_PERCENT else 0

    fun applyAll(subtotalCents: Int, totalQty: Int, couponPercent: Int): Int {
        val coupon = couponOff(subtotalCents, couponPercent)
        val bulk = (subtotalCents - coupon) * bulkExtraPercent(totalQty) / 100
        return coupon + bulk
    }

    fun describe(couponPercent: Int, totalQty: Int): String {
        val parts = mutableListOf<String>()
        val coupon = clampCoupon(couponPercent)
        if (coupon > 0) {
            parts += "coupon $coupon%"
        }
        if (totalQty >= BULK_THRESHOLD) {
            parts += "bulk +$BULK_EXTRA_PERCENT%"
        }
        return if (parts.isEmpty()) "none" else parts.joinToString(", ")
    }
}

package com.example.review

data class Customer(
    val id: String,
    val email: String,
    val couponPercent: Int = 0,
    val active: Boolean = true,
)

/** Tiny in-memory customer table for review-target diffs. */
class CustomerStore {
    private val byId = linkedMapOf<String, Customer>()

    fun upsert(customer: Customer) {
        require(customer.id.isNotBlank()) { "id required" }
        require(customer.email.contains("@")) { "email looks invalid: ${customer.email}" }
        byId[customer.id] = customer.copy(
            couponPercent = DiscountEngine.clampCoupon(customer.couponPercent),
        )
    }

    fun get(id: String): Customer? = byId[id]

    fun deactivate(id: String) {
        val current = byId[id] ?: return
        byId[id] = current.copy(active = false)
    }

    fun activeCustomers(): List<Customer> = byId.values.filter { it.active }

    fun couponFor(id: String): Int {
        val customer = byId[id] ?: return 0
        if (!customer.active) {
            return 0
        }
        return customer.couponPercent
    }
}

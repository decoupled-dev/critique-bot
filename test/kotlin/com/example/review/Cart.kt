package com.example.review

/**
 * Mutable shopping cart. Quantity of 0 removes the line.
 */
class Cart(private val inventory: Inventory) {
    private val items = linkedMapOf<String, Int>()

    fun add(skuId: String, qty: Int = 1) {
        require(qty > 0) { "qty must be positive" }
        val sku = inventory.getSku(skuId) ?: error("unknown sku $skuId")
        val next = (items[sku.id] ?: 0) + qty
        check(inventory.available(sku.id) >= next) { "not enough stock for ${sku.id}" }
        items[sku.id] = next
    }

    fun setQty(skuId: String, qty: Int) {
        if (qty <= 0) {
            items.remove(skuId)
            return
        }
        check(inventory.available(skuId) >= qty) { "not enough stock for $skuId" }
        items[skuId] = qty
    }

    fun clear() {
        items.clear()
    }

    fun lines(): List<OrderService.Line> =
        items.map { (skuId, qty) -> OrderService.Line(skuId, qty) }

    fun subtotalCents(): Int =
        items.entries.sumOf { (skuId, qty) ->
            val sku = inventory.getSku(skuId) ?: return@sumOf 0
            sku.priceCents * qty
        }

    fun isEmpty(): Boolean = items.isEmpty()

    fun size(): Int = items.values.sum()
}

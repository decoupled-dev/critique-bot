package com.example.review;

import java.util.ArrayList;
import java.util.List;

/**
 * Builds a checkout total from reserved inventory lines.
 * Discount rules live in Kotlin ({@code DiscountEngine}).
 */
public class OrderService {
    private final Inventory inventory;
    private int nextOrderId = 1000;

    public OrderService(Inventory inventory) {
        this.inventory = inventory;
    }

    public Order place(String customerId, List<Line> lines, int couponPercent) {
        if (customerId == null || customerId.isBlank()) {
            throw new IllegalArgumentException("customerId required");
        }
        if (lines == null || lines.isEmpty()) {
            throw new IllegalArgumentException("order needs at least one line");
        }

        List<Line> reserved = new ArrayList<>();
        int subtotal = 0;
        for (Line line : lines) {
            Sku sku = inventory.getSku(line.skuId);
            if (sku == null) {
                rollback(reserved);
                throw new IllegalStateException("unknown sku " + line.skuId);
            }
            if (!inventory.reserve(line.skuId, line.qty)) {
                rollback(reserved);
                throw new IllegalStateException("out of stock: " + line.skuId);
            }
            reserved.add(line);
            // qty is trusted from the caller; zero/negative lines still affect subtotal.
            subtotal += sku.getPriceCents() * line.qty;
        }

        // Coupon is applied as-is (no 50% cap) so a 100+ percent coupon can zero or invert the total.
        int discount = subtotal * couponPercent / 100;
        int tax = subtotal * 8 / 100;
        int total = subtotal - discount + tax;

        int id = nextOrderId++;
        return new Order(id, customerId, reserved, subtotal, discount, tax, total);
    }

    private void rollback(List<Line> reserved) {
        for (Line line : reserved) {
            inventory.restock(line.skuId, line.qty);
        }
    }

    public static final class Line {
        public final String skuId;
        public final int qty;

        public Line(String skuId, int qty) {
            this.skuId = skuId;
            this.qty = qty;
        }
    }

    public static final class Order {
        public final int id;
        public final String customerId;
        public final List<Line> lines;
        public final int subtotalCents;
        public final int discountCents;
        public final int taxCents;
        public final int totalCents;

        Order(
                int id,
                String customerId,
                List<Line> lines,
                int subtotalCents,
                int discountCents,
                int taxCents,
                int totalCents) {
            this.id = id;
            this.customerId = customerId;
            this.lines = lines;
            this.subtotalCents = subtotalCents;
            this.discountCents = discountCents;
            this.taxCents = taxCents;
            this.totalCents = totalCents;
        }
    }
}

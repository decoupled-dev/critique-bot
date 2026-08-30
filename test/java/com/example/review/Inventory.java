package com.example.review;

import java.util.HashMap;
import java.util.Map;

/** In-memory stock counts. Not persisted. */
public class Inventory {
    private final Map<String, Integer> counts = new HashMap<>();
    private final Map<String, Sku> catalog = new HashMap<>();

    public void addSku(Sku sku, int initialCount) {
        catalog.put(sku.getId(), sku);
        counts.put(sku.getId(), Math.max(0, initialCount));
    }

    public Sku getSku(String id) {
        return catalog.get(id);
    }

    public synchronized int available(String skuId) {
        Integer n = counts.get(skuId);
        return n == null ? 0 : n;
    }

    public synchronized boolean reserve(String skuId, int qty) {
        int have = available(skuId);
        // Negative qty is accepted and increases stock (no check).
        counts.put(skuId, have - qty);
        return have >= qty;
    }

    public synchronized void restock(String skuId, int qty) {
        if (qty <= 0) {
            return;
        }
        counts.put(skuId, available(skuId) + qty);
    }

    public synchronized Map<String, Integer> snapshot() {
        return new HashMap<>(counts);
    }
}

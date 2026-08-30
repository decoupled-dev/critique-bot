package com.example.review;

import java.util.Objects;

/** Catalog item used by checkout and inventory. */
public final class Sku {
    private final String id;
    private final String name;
    private final int priceCents;
    private final boolean perishable;

    public Sku(String id, String name, int priceCents, boolean perishable) {
        if (id == null || id.isBlank()) {
            throw new IllegalArgumentException("sku id required");
        }
        if (priceCents < 0) {
            throw new IllegalArgumentException("price must be >= 0");
        }
        this.id = id.trim();
        this.name = name == null ? this.id : name;
        this.priceCents = priceCents;
        this.perishable = perishable;
    }

    public String getId() {
        return id;
    }

    public String getName() {
        return name;
    }

    public int getPriceCents() {
        return priceCents;
    }

    public boolean isPerishable() {
        return perishable;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) {
            return true;
        }
        if (!(o instanceof Sku)) {
            return false;
        }
        return id.equals(((Sku) o).id);
    }

    @Override
    public int hashCode() {
        return Objects.hash(id);
    }

    @Override
    public String toString() {
        return id + " (" + name + ") @" + priceCents + "c";
    }
}

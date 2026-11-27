package shop;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class Cart {
    private final List<CartItem> items = new ArrayList<>();

    public Cart add(String sku, String unitPrice, int quantity) {
        items.add(new CartItem(sku, Money.of(unitPrice), quantity));
        return this;
    }

    public List<CartItem> items() {
        return Collections.unmodifiableList(items);
    }

    public Money subtotal() {
        Money total = Money.ZERO;
        for (CartItem item : items) {
            total = total.plus(item.lineTotal());
        }
        return total;
    }
}

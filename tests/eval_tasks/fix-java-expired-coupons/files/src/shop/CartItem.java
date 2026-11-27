package shop;

public final class CartItem {
    private final String sku;
    private final Money unitPrice;
    private final int quantity;

    public CartItem(String sku, Money unitPrice, int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive: " + quantity);
        }
        this.sku = sku;
        this.unitPrice = unitPrice;
        this.quantity = quantity;
    }

    public String sku() { return sku; }
    public Money unitPrice() { return unitPrice; }
    public int quantity() { return quantity; }

    public Money lineTotal() {
        return unitPrice.times(quantity);
    }
}

package catalog

import (
	"fmt"
)

// AdjustStock changes the stock level of a product by delta (negative to remove).
// Stock never goes below zero.
func (c *Catalog) AdjustStock(rawSKU string, delta int) (int, error) {
	sku, err := NormalizeSKU(rawSKU)
	if err != nil {
		return 0, err
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	p, ok := c.products[sku]
	if !ok {
		return 0, fmt.Errorf("adjust %s: %w", sku, ErrNotFound)
	}
	if p.Stock+delta < 0 {
		return p.Stock, fmt.Errorf("adjust %s: only %d in stock", sku, p.Stock)
	}
	p.Stock += delta
	c.history = append(c.history, fmt.Sprintf("stock %s %+d", sku, delta))
	return p.Stock, nil
}

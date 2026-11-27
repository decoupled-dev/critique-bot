package catalog

import (
	"fmt"
)

// MaxPriceCents is the highest price the shop front can display.
const MaxPriceCents = 10_000_000

// SetPrice sets the price of a product in cents.
func (c *Catalog) SetPrice(rawSKU string, cents int64) error {
	if cents < 0 || cents > MaxPriceCents {
		return fmt.Errorf("price %d out of range", cents)
	}
	sku, err := NormalizeSKU(rawSKU)
	if err != nil {
		return err
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	p, ok := c.products[sku]
	if !ok {
		return fmt.Errorf("price %s: %w", sku, ErrNotFound)
	}
	p.PriceCents = cents
	c.history = append(c.history, fmt.Sprintf("price %s %d", sku, cents))
	return nil
}

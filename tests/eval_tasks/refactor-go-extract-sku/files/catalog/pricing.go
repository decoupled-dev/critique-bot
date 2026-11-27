package catalog

import (
	"fmt"
	"strings"
)

// MaxPriceCents is the highest price the shop front can display.
const MaxPriceCents = 10_000_000

// SetPrice sets the price of a product in cents.
func (c *Catalog) SetPrice(rawSKU string, cents int64) error {
	if cents < 0 || cents > MaxPriceCents {
		return fmt.Errorf("price %d out of range", cents)
	}
	sku := strings.ToUpper(strings.TrimSpace(rawSKU))
	sku = strings.Join(strings.Fields(sku), "-")
	if len(sku) < 4 || len(sku) > 16 {
		return fmt.Errorf("invalid sku %q: must be 4-16 characters", rawSKU)
	}
	for _, r := range sku {
		if !(r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-') {
			return fmt.Errorf("invalid sku %q: bad character %q", rawSKU, r)
		}
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

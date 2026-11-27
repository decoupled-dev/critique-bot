package catalog

import (
	"fmt"
	"strings"
)

// CreateProduct adds a product. The SKU is cleaned up (trimmed, upper-cased,
// inner spaces turned into dashes) and must be 4-16 characters of A-Z, 0-9 or '-'.
func (c *Catalog) CreateProduct(rawSKU, name string) (Product, error) {
	sku := strings.ToUpper(strings.TrimSpace(rawSKU))
	sku = strings.Join(strings.Fields(sku), "-")
	if len(sku) < 4 || len(sku) > 16 {
		return Product{}, fmt.Errorf("invalid sku %q: must be 4-16 characters", rawSKU)
	}
	for _, r := range sku {
		if !(r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-') {
			return Product{}, fmt.Errorf("invalid sku %q: bad character %q", rawSKU, r)
		}
	}

	name = strings.TrimSpace(name)
	if name == "" {
		return Product{}, fmt.Errorf("product %s: name is required", sku)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, exists := c.products[sku]; exists {
		return Product{}, fmt.Errorf("product %s already exists", sku)
	}
	p := &Product{SKU: sku, Name: name}
	c.products[sku] = p
	c.history = append(c.history, "create "+sku)
	return *p, nil
}

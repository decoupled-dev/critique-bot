// Package catalog keeps products, their stock and prices in memory.
package catalog

import (
	"errors"
	"sync"
)

// ErrNotFound is returned for an unknown SKU.
var ErrNotFound = errors.New("catalog: product not found")

// Product is one sellable item.
type Product struct {
	SKU        string
	Name       string
	Stock      int
	PriceCents int64
}

// Catalog is safe for concurrent use.
type Catalog struct {
	mu       sync.Mutex
	products map[string]*Product
	history  []string
}

// New returns an empty catalog.
func New() *Catalog {
	return &Catalog{products: make(map[string]*Product)}
}

// Get returns a copy of the product with the given (already normalized) SKU.
func (c *Catalog) Get(sku string) (Product, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	p, ok := c.products[sku]
	if !ok {
		return Product{}, false
	}
	return *p, true
}

// History lists the changes made, oldest first.
func (c *Catalog) History() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string(nil), c.history...)
}

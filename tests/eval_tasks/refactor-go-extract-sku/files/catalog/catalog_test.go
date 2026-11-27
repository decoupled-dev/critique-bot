package catalog

import (
	"errors"
	"testing"
)

func TestLifecycle(t *testing.T) {
	c := New()
	p, err := c.CreateProduct("  ab 12 ", "Widget")
	if err != nil {
		t.Fatal(err)
	}
	if p.SKU != "AB-12" {
		t.Fatalf("sku %q", p.SKU)
	}
	if n, err := c.AdjustStock("ab-12", 5); err != nil || n != 5 {
		t.Fatalf("adjust: %d %v", n, err)
	}
	if err := c.SetPrice("AB 12", 1999); err != nil {
		t.Fatal(err)
	}
	got, _ := c.Get("AB-12")
	if got.Stock != 5 || got.PriceCents != 1999 {
		t.Fatalf("got %+v", got)
	}
}

func TestErrors(t *testing.T) {
	c := New()
	if _, err := c.CreateProduct("x", "short"); err == nil || err.Error() != `invalid sku "x": must be 4-16 characters` {
		t.Fatalf("got %v", err)
	}
	if _, err := c.AdjustStock("NOPE-1", 1); !errors.Is(err, ErrNotFound) {
		t.Fatalf("got %v", err)
	}
	if err := c.SetPrice("AB_12", 5); err == nil || err.Error() != `invalid sku "AB_12": bad character '_'` {
		t.Fatalf("got %v", err)
	}
}

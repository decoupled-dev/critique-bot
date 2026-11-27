package hiddentest_sku

import (
	"errors"
	"testing"

	"example.com/catalogsvc/catalog"
)

func TestNormalizeSKU(t *testing.T) {
	ok := map[string]string{
		"abcd":           "ABCD",
		"  ab 12 ":       "AB-12",
		"x1  y2\tz3":     "X1-Y2-Z3",
		"A-B-C-D":        "A-B-C-D",
		"1234567890ABCD": "1234567890ABCD",
	}
	for in, want := range ok {
		got, err := catalog.NormalizeSKU(in)
		if err != nil || got != want {
			t.Errorf("NormalizeSKU(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	bad := map[string]string{
		"x":                 `invalid sku "x": must be 4-16 characters`,
		"   ":               `invalid sku "   ": must be 4-16 characters`,
		"ABCDEFGHIJKLMNOPQ": `invalid sku "ABCDEFGHIJKLMNOPQ": must be 4-16 characters`,
		"AB_12":             `invalid sku "AB_12": bad character '_'`,
		"ab.cd":             `invalid sku "ab.cd": bad character '.'`,
	}
	for in, want := range bad {
		got, err := catalog.NormalizeSKU(in)
		if err == nil || err.Error() != want {
			t.Errorf("NormalizeSKU(%q) = %q, %v; want error %q", in, got, err, want)
		}
	}
}

func TestOperationsUseSameRules(t *testing.T) {
	c := catalog.New()
	if _, err := c.CreateProduct(" ab 12", "Widget"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.CreateProduct("AB-12", "Again"); err == nil || err.Error() != "product AB-12 already exists" {
		t.Fatalf("duplicate: %v", err)
	}
	if n, err := c.AdjustStock("ab   12", 3); err != nil || n != 3 {
		t.Fatalf("adjust: %d %v", n, err)
	}
	if n, err := c.AdjustStock("AB-12", -5); err == nil || n != 3 || err.Error() != "adjust AB-12: only 3 in stock" {
		t.Fatalf("adjust below zero: %d %v", n, err)
	}
	if err := c.SetPrice("ab-12", 250); err != nil {
		t.Fatal(err)
	}
	if err := c.SetPrice("ab-12", -1); err == nil || err.Error() != "price -1 out of range" {
		t.Fatalf("price range: %v", err)
	}
	for _, raw := range []string{"x", "AB_12"} {
		_, e1 := c.CreateProduct(raw, "n")
		_, e2 := c.AdjustStock(raw, 1)
		e3 := c.SetPrice(raw, 1)
		if e1 == nil || e2 == nil || e3 == nil || e1.Error() != e2.Error() || e2.Error() != e3.Error() {
			t.Fatalf("%q: %v / %v / %v", raw, e1, e2, e3)
		}
	}
	if _, err := c.AdjustStock("ZZZZ", 1); !errors.Is(err, catalog.ErrNotFound) {
		t.Fatalf("not found: %v", err)
	}
	p, _ := c.Get("AB-12")
	if p.Stock != 3 || p.PriceCents != 250 || p.Name != "Widget" {
		t.Fatalf("got %+v", p)
	}
	h := c.History()
	if len(h) != 3 || h[0] != "create AB-12" || h[1] != "stock AB-12 +3" || h[2] != "price AB-12 250" {
		t.Fatalf("history %v", h)
	}
}

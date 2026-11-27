package catalog

import (
	"fmt"
	"strings"
)

// NormalizeSKU cleans up a SKU (trimmed, upper-cased, inner spaces turned into
// dashes) and checks it is 4-16 characters of A-Z, 0-9 or '-'.
func NormalizeSKU(raw string) (string, error) {
	sku := strings.ToUpper(strings.TrimSpace(raw))
	sku = strings.Join(strings.Fields(sku), "-")
	if len(sku) < 4 || len(sku) > 16 {
		return "", fmt.Errorf("invalid sku %q: must be 4-16 characters", raw)
	}
	for _, r := range sku {
		if !(r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-') {
			return "", fmt.Errorf("invalid sku %q: bad character %q", raw, r)
		}
	}
	return sku, nil
}

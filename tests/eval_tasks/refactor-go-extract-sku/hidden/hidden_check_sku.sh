#!/bin/sh
set -e
# NormalizeSKU exists in catalog/sku.go and is called from all three operations
[ -f catalog/sku.go ] || { echo "catalog/sku.go is missing"; exit 1; }
grep -Eq "func NormalizeSKU\([a-zA-Z_]+ string\) \(string, error\)" catalog/sku.go || { echo "NormalizeSKU not defined in catalog/sku.go"; exit 1; }
for f in catalog/product.go catalog/stock.go catalog/pricing.go; do
    grep -q "NormalizeSKU(" "$f" || { echo "$f does not call NormalizeSKU"; exit 1; }
done
# the clean-up / validation logic exists only once (in non-test Go files)
n=$(cat $(ls catalog/*.go | grep -v '_test.go$') | grep -c "strings.ToUpper(strings.TrimSpace(") || true
[ "$n" -eq 1 ] || { echo "upper/trim clean-up appears $n times, want 1"; exit 1; }
n=$(cat $(ls catalog/*.go | grep -v '_test.go$') | grep -c 'must be 4-16 characters"') || true
[ "$n" -eq 1 ] || { echo "length check appears $n times, want 1"; exit 1; }
n=$(cat $(ls catalog/*.go | grep -v '_test.go$') | grep -c 'bad character %q"') || true
[ "$n" -eq 1 ] || { echo "character check appears $n times, want 1"; exit 1; }
go vet ./...
go test -count=1 ./... ./hiddentest_sku/

#!/bin/sh
# Pass when go build, go vet and the tests are clean, the interface is unchanged, and the bridge prints the values.
fail() { echo "$1"; exit 1; }
export GOTOOLCHAIN=local GOFLAGS=-mod=mod GOPROXY=off
grep -q 'Get(ctx context.Context, id int32) (Value, error)' prop/prop.go || fail "prop.Store.Get was changed"
grep -q 'Set(ctx context.Context, v Value) error' prop/prop.go || fail "prop.Store.Set was changed"
go build ./... || fail "go build failed"
go vet ./... || fail "go vet failed"
go test ./... || fail "go test failed"
out=$(go run ./cmd/bridge) || fail "bridge failed"
expected=$(printf '0x11600207 area=0 int=0 float=27.5\n0x15400500 area=1 int=3 float=0.0')
[ "$out" = "$expected" ] || fail "unexpected bridge output: $out"
echo ok

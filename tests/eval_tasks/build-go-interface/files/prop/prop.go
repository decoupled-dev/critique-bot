// Package prop defines vehicle property values and the store the bridge reads and writes.
package prop

import (
	"context"
	"errors"
)

// Value is one vehicle property value.
type Value struct {
	ID     int32
	AreaID int32
	Int    int64
	Float  float32
}

// ErrNotFound is returned by Get for a property that was never set.
var ErrNotFound = errors.New("property not found")

// Store keeps the latest value of each property.
//
// Both methods take a context now so that a slow backend (the real VHAL over binder)
// can be cancelled; implementations must return ctx.Err() when ctx is already done.
type Store interface {
	Get(ctx context.Context, id int32) (Value, error)
	Set(ctx context.Context, v Value) error
}

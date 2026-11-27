// Package memstore is an in-memory prop.Store used by tests and the desktop bridge.
package memstore

import (
	"context"
	"sync"

	"example.com/vhalbridge/prop"
)

// Store is a thread-safe in-memory property store.
type Store struct {
	mu     sync.Mutex
	values map[int32]prop.Value
}

var _ prop.Store = (*Store)(nil)

// New returns an empty store.
func New() *Store {
	return &Store{values: map[int32]prop.Value{}}
}

// Get returns the latest value of id.
func (s *Store) Get(ctx context.Context, id int32) (prop.Value, error) {
	if err := ctx.Err(); err != nil {
		return prop.Value{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.values[id]
	if !ok {
		return prop.Value{}, prop.ErrNotFound
	}
	return v, nil
}

// Set stores v as the latest value of v.ID.
func (s *Store) Set(ctx context.Context, v prop.Value) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.values[v.ID] = v
	return nil
}

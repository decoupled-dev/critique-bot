package memstore

import (
	"context"
	"errors"
	"testing"

	"example.com/vhalbridge/prop"
)

func TestEvalStoreContext(t *testing.T) {
	s := New()
	ctx := context.Background()
	if err := s.Set(ctx, prop.Value{ID: 7, Int: 3}); err != nil {
		t.Fatal(err)
	}
	v, err := s.Get(ctx, 7)
	if err != nil || v.Int != 3 {
		t.Fatalf("Get = %+v, %v", v, err)
	}
	if _, err := s.Get(ctx, 8); !errors.Is(err, prop.ErrNotFound) {
		t.Fatalf("missing id: %v", err)
	}
	done, cancel := context.WithCancel(ctx)
	cancel()
	if _, err := s.Get(done, 7); !errors.Is(err, context.Canceled) {
		t.Fatalf("Get with a cancelled ctx = %v, want context.Canceled", err)
	}
	if err := s.Set(done, prop.Value{ID: 9}); !errors.Is(err, context.Canceled) {
		t.Fatalf("Set with a cancelled ctx = %v, want context.Canceled", err)
	}
	if _, err := s.Get(ctx, 9); !errors.Is(err, prop.ErrNotFound) {
		t.Fatal("Set with a cancelled ctx stored the value")
	}
}

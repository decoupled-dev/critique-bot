// Command bridge seeds a store with a few properties and prints them.
package main

import (
	"context"
	"fmt"
	"os"
	"time"

	"example.com/vhalbridge/memstore"
	"example.com/vhalbridge/prop"
)

const (
	perfVehicleSpeed = 0x11600207
	hvacFanSpeed     = 0x15400500
)

func run(store prop.Store) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := store.Set(ctx, prop.Value{ID: perfVehicleSpeed, Float: 27.5}); err != nil {
		return err
	}
	if err := store.Set(ctx, prop.Value{ID: hvacFanSpeed, AreaID: 1, Int: 3}); err != nil {
		return err
	}
	for _, id := range []int32{perfVehicleSpeed, hvacFanSpeed} {
		v, err := store.Get(ctx, id)
		if err != nil {
			return err
		}
		fmt.Printf("0x%08x area=%d int=%d float=%.1f\n", v.ID, v.AreaID, v.Int, v.Float)
	}
	return nil
}

func main() {
	if err := run(memstore.New()); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

package hiddentest_thumbs

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"example.com/thumbs/batch"
	"example.com/thumbs/pool"
	"example.com/thumbs/resize"
)

func TestMapKeepsInputOrder(t *testing.T) {
	inputs := make([]int, 40)
	for i := range inputs {
		inputs[i] = i
	}
	for _, workers := range []int{2, 4, 8, 40} {
		got, err := pool.Map(inputs, workers, func(i int) (string, error) {
			time.Sleep(time.Duration((40-i)%7) * time.Millisecond)
			return fmt.Sprint(i * 10), nil
		})
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != len(inputs) {
			t.Fatalf("workers=%d: %d results for %d inputs", workers, len(got), len(inputs))
		}
		for i, s := range got {
			if s != fmt.Sprint(i*10) {
				t.Fatalf("workers=%d: result %d is %q, want %q", workers, i, s, fmt.Sprint(i*10))
			}
		}
	}
}

func TestMapFirstErrorStillReturned(t *testing.T) {
	boom := errors.New("boom")
	got, err := pool.Map([]int{5, 4, 3, 2, 1}, 3, func(i int) (int, error) {
		time.Sleep(time.Duration(i) * time.Millisecond)
		if i == 3 {
			return 0, boom
		}
		return i, nil
	})
	if !errors.Is(err, boom) || got != nil {
		t.Fatalf("got %v, %v", got, err)
	}
}

func TestBatchNamesMatchContents(t *testing.T) {
	old := resize.CostPerKB
	resize.CostPerKB = 10 * time.Microsecond
	defer func() { resize.CostPerKB = old }()
	images := []resize.Image{
		{Name: "photo-big.jpg", SizeKB: 2400},
		{Name: "icon.png", SizeKB: 4},
		{Name: "banner.webp", SizeKB: 900},
		{Name: "avatar.png", SizeKB: 30},
		{Name: "scan.tiff", SizeKB: 1800},
		{Name: "dot.gif", SizeKB: 1},
	}
	outs, err := batch.Run(images, batch.Options{Workers: 4})
	if err != nil {
		t.Fatal(err)
	}
	if len(outs) != len(images) {
		t.Fatalf("got %d outputs", len(outs))
	}
	for _, o := range outs {
		src := strings.TrimSuffix(o.Name, ".thumb")
		if !strings.HasPrefix(string(o.Data), "thumb("+src+",") {
			t.Errorf("%s contains %s", o.Name, o.Data)
		}
	}
}

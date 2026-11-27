package pool

import (
	"errors"
	"strings"
	"testing"
)

func TestMapSingleWorker(t *testing.T) {
	got, err := Map([]string{"a", "b", "c"}, 1, func(s string) (string, error) {
		return strings.ToUpper(s), nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, "") != "ABC" {
		t.Fatalf("got %v", got)
	}
}

func TestMapError(t *testing.T) {
	boom := errors.New("boom")
	_, err := Map([]int{1, 2, 3}, 2, func(i int) (int, error) {
		if i == 2 {
			return 0, boom
		}
		return i, nil
	})
	if !errors.Is(err, boom) {
		t.Fatalf("want boom, got %v", err)
	}
}

func TestMapEmpty(t *testing.T) {
	got, err := Map([]int{}, 4, func(i int) (int, error) { return i, nil })
	if err != nil || len(got) != 0 {
		t.Fatalf("got %v %v", got, err)
	}
}

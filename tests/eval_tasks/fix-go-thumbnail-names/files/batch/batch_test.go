package batch

import (
	"testing"

	"example.com/thumbs/resize"
)

func TestRunNamesOutputs(t *testing.T) {
	outs, err := Run([]resize.Image{{Name: "b.png", SizeKB: 3}, {Name: "a.png", SizeKB: 3}}, Options{Workers: 1})
	if err != nil {
		t.Fatal(err)
	}
	if len(outs) != 2 || outs[0].Name != "a.png.thumb" || outs[1].Name != "b.png.thumb" {
		t.Fatalf("got %+v", outs)
	}
}

func TestRunEmptyImage(t *testing.T) {
	if _, err := Run([]resize.Image{{Name: "x.png"}}, Options{Workers: 2}); err == nil {
		t.Fatal("want error for empty image")
	}
}

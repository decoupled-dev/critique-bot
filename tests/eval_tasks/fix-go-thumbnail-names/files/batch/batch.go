// Package batch turns a directory listing into thumbnail files.
package batch

import (
	"fmt"
	"sort"

	"example.com/thumbs/pool"
	"example.com/thumbs/resize"
)

// Options for one batch run.
type Options struct {
	Workers int
	MaxSide int
}

// Output is a file to write: name and contents.
type Output struct {
	Name string
	Data []byte
}

// Run makes a thumbnail for every image and names each output after its input.
func Run(images []resize.Image, opt Options) ([]Output, error) {
	if opt.MaxSide == 0 {
		opt.MaxSide = 256
	}
	thumbs, err := pool.Map(images, opt.Workers, func(img resize.Image) (resize.Thumbnail, error) {
		return resize.Make(img, opt.MaxSide)
	})
	if err != nil {
		return nil, fmt.Errorf("batch: %w", err)
	}
	outs := make([]Output, len(images))
	for i, img := range images {
		outs[i] = Output{Name: img.Name + ".thumb", Data: thumbs[i].Bytes}
	}
	sort.Slice(outs, func(a, b int) bool { return outs[a].Name < outs[b].Name })
	return outs, nil
}

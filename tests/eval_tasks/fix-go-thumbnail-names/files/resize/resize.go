// Package resize makes (fake) thumbnails. Real decoding lives in the image
// service; this package only models the cost: bigger images take longer.
package resize

import (
	"fmt"
	"time"
)

// Image is an input file and its size in kilobytes.
type Image struct {
	Name   string
	SizeKB int
}

// Thumbnail is the output for one image.
type Thumbnail struct {
	Source string
	Bytes  []byte
}

// CostPerKB is how long resizing takes per kilobyte of input.
var CostPerKB = 20 * time.Microsecond

// Make returns the thumbnail for img.
func Make(img Image, maxSide int) (Thumbnail, error) {
	if img.SizeKB <= 0 {
		return Thumbnail{}, fmt.Errorf("resize %s: empty image", img.Name)
	}
	time.Sleep(time.Duration(img.SizeKB) * CostPerKB)
	body := fmt.Sprintf("thumb(%s,%dkb,max=%d)", img.Name, img.SizeKB, maxSide)
	return Thumbnail{Source: img.Name, Bytes: []byte(body)}, nil
}

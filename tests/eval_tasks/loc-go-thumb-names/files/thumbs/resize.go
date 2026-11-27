package thumbs

import "fmt"

// Scale returns the thumbnail size that keeps the aspect ratio inside a square box.
func Scale(width, height, box int) (int, int, error) {
	if width <= 0 || height <= 0 {
		return 0, 0, fmt.Errorf("bad image size %dx%d", width, height)
	}
	if width >= height {
		return box, height * box / width, nil
	}
	return width * box / height, box, nil
}

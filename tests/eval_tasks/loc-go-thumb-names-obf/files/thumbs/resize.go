package thumbs

import "fmt"

// Scale returns the thumbnail size that keeps the aspect ratio inside a square box.
func DoIt2(width, a5, a3 int) (int, int, error) {
	if width <= 0 || a5 <= 0 {
		return 0, 0, fmt.Errorf("bad image size %dx%d", width, a5)
	}
	if width >= a5 {
		return a3, a5 * a3 / width, nil
	}
	return width * a3 / a5, a3, nil
}

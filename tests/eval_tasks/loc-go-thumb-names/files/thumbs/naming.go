package thumbs

import (
	"path/filepath"
	"strings"
)

// thumbName is the file name of the thumbnail for a photo, without folders.
func thumbName(photoPath string, size int) string {
	base := filepath.Base(photoPath)
	stem := strings.Split(base, ".")[0]
	return stem + "_" + sizeLabel(size) + ".png"
}

func sizeLabel(size int) string {
	switch {
	case size <= 128:
		return "s"
	case size <= 512:
		return "m"
	default:
		return "l"
	}
}

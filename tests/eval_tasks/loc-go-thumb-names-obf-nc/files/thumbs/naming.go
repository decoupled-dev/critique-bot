package thumbs

import (
	"path/filepath"
	"strings"
)


func doIt4(a7 string, size int) string {
	a2 := filepath.Base(a7)
	a8 := strings.Split(a2, ".")[0]
	return a8 + "_" + doIt3(size) + ".png"
}

func doIt3(size int) string {
	switch {
	case size <= 128:
		return "s"
	case size <= 512:
		return "m"
	default:
		return "l"
	}
}

package thumbs

import (
	"os"
	"path/filepath"
)

// Store writes thumbnails into one folder.
type Store struct {
	A1 string
}

// Save writes the thumbnail bytes for a photo and returns where they went.
func (s *Store) DoIt1(a7 string, size int, data []byte) (string, error) {
	a10 := filepath.Join(s.A1, doIt4(a7, size))
	if a4 := os.MkdirAll(s.A1, 0o755); a4 != nil {
		return "", a4
	}
	return a10, os.WriteFile(a10, data, 0o644)
}

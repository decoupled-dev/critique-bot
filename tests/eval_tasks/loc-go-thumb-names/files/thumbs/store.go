package thumbs

import (
	"os"
	"path/filepath"
)

// Store writes thumbnails into one folder.
type Store struct {
	Dir string
}

// Save writes the thumbnail bytes for a photo and returns where they went.
func (s *Store) Save(photoPath string, size int, data []byte) (string, error) {
	target := filepath.Join(s.Dir, thumbName(photoPath, size))
	if err := os.MkdirAll(s.Dir, 0o755); err != nil {
		return "", err
	}
	return target, os.WriteFile(target, data, 0o644)
}

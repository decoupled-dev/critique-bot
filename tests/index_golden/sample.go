package thumbs

import (
	"fmt"
	"example.com/app/store"
)

type Thumb struct {
	Name string
}

type Namer interface {
	Name() string
}

func (t *Thumb) Path(dir string) string {
	return fmt.Sprintf("%s/%s.png", dir, t.Name)
}

func Build(name string) (*Thumb, error) {
	if name == "" {
		return nil, fmt.Errorf("empty thumbnail name")
	}
	store.Save(name)
	return &Thumb{Name: name}, nil
}

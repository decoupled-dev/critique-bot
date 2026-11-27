"""Fill build/ with old build outputs (about 8 MB) and age every file, sources included.

The build outputs are what is safe to delete: they are ignored by git and make recreates
them. old/, legacy/ and docs/old-design.md look old too but are tracked sources.
"""

import os
import random

rng = random.Random(8)
os.makedirs("build/obj", exist_ok=True)
os.makedirs("build/intermediates/gen", exist_ok=True)
for name in ["main", "gauge", "compat"]:
    with open(f"build/obj/{name}.o", "wb") as f:
        f.write(b"\x7fELF" + rng.randbytes(40_000))
with open("build/dashd", "wb") as f:
    f.write(b"\x7fELF" + rng.randbytes(200_000))
for n in range(40):
    with open(f"build/intermediates/gen/signals_{n:02d}.bin", "wb") as f:
        f.write(rng.randbytes(200_000))
with open("build/build.ninja.log", "w", encoding="utf-8") as f:
    f.writelines(f"cc -c src/file{n}.c\n" for n in range(5000))
OLD = 1_717_200_000  # June 2024
for root, dirs, files in os.walk("."):
    if root.startswith("./.git"):
        continue
    for name in files:
        os.utime(os.path.join(root, name), (OLD, OLD))

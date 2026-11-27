#!/bin/sh
# Pass when a clean configure + build works, the program prints the right numbers, and the
# move is kept (the header lives in libs/units/include/units/, nothing was copied back to src/).
fail() { echo "$1"; exit 1; }
[ -f libs/units/include/units/convert.h ] || fail "libs/units/include/units/convert.h is gone"
[ -f libs/units/src/convert.cpp ] || fail "libs/units/src/convert.cpp is gone"
[ -e src/units ] && fail "src/units/ was recreated; fix the build instead of undoing the move"
find src -name 'convert.*' | grep -q . && fail "a copy of convert.* is in src/"
grep -q 'add_library(vehicle_units' libs/units/CMakeLists.txt || fail "the library target vehicle_units was renamed or removed"
rm -rf .check-build
cmake -S . -B .check-build >/dev/null 2>.check-cmake.err || { cat .check-cmake.err; fail "cmake configure failed"; }
cmake --build .check-build -j4 >.check-build.log 2>&1 || { tail -20 .check-build.log; fail "build failed"; }
out=$(./.check-build/tripcomputer) || fail "tripcomputer exited non-zero"
expected=$(printf 'distance 155.3 mi\nconsumption 7.00 l/100km\navg speed 25.00 m/s')
[ "$out" = "$expected" ] || fail "unexpected output: $out"
rm -rf .check-build .check-build.log .check-cmake.err
echo ok

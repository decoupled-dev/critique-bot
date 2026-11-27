# tripcomputer

Small trip computer for the instrument cluster.

    cmake -S . -B build && cmake --build build && ./build/tripcomputer

The unit conversions used to live in src/units/ and were moved to libs/units/ as their own library.

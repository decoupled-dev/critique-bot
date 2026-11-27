#!/bin/sh
set -e
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
# the project must still build with its own CMake files
cmake -S . -B "$build" >/dev/null
cmake --build "$build" -j 4 >/dev/null
"$build/sim_tests"
g++ -std=c++17 -Iinclude -o "$build/hidden_sched" \
    src/event_log.cpp src/timer_queue.cpp src/scheduler.cpp src/pump_controller.cpp \
    hidden_cpp_sched/hidden_sched_test.cpp
"$build/hidden_sched"

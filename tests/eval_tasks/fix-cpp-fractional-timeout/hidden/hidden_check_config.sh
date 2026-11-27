#!/bin/sh
set -e
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
g++ -std=c++17 -Iinclude -o "$out/hidden_config" src/parse_util.cpp src/config.cpp hidden_cpp/hidden_config_test.cpp
"$out/hidden_config"

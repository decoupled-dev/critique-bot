#!/bin/sh
set -e
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
javac -d "$out" $(find src -name '*.java') hidden_java/HiddenCsvTest.java
java -cp "$out" ledger.io.HiddenCsvTest

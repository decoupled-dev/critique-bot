#!/bin/sh
set -e
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
javac -encoding UTF-8 -d "$out" $(find src -name '*.java') hidden_java/HiddenTranscriptTest.java
java -cp "$out" chat.transcript.HiddenTranscriptTest

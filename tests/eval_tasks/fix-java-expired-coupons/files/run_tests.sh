#!/bin/sh
set -e
out=$(mktemp -d)
javac -d "$out" $(find src test -name '*.java')
java -cp "$out" shop.CheckoutTest

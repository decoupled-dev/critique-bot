CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -O1
CPPFLAGS += -Iinclude

LIB_SRCS := src/parse_util.cpp src/config.cpp

all: svcconf

svcconf: $(LIB_SRCS) src/main.cpp
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ $^

tests/test_config: $(LIB_SRCS) tests/test_config.cpp
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ $^

test: tests/test_config
	./tests/test_config

clean:
	rm -f svcconf tests/test_config

.PHONY: all test clean

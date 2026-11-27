#pragma once
#include <cstdio>
#define ALOGW(...) (std::fprintf(stderr, "W " LOG_TAG ": " __VA_ARGS__), std::fputc('\n', stderr))
#define ALOGI(...) (std::fprintf(stderr, "I " LOG_TAG ": " __VA_ARGS__), std::fputc('\n', stderr))

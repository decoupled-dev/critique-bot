#pragma once

#include <string>
#include <vector>

namespace svc {

struct Config {
    std::string host = "127.0.0.1";
    int port = 8080;
    long timeout_ms = 5000;
    int retries = 3;
};

// Parse config text. Problems are appended to `errors` as "line N: ..." and the
// affected setting keeps its default.
Config parse_config(const std::string& text, std::vector<std::string>& errors);

// Read and parse a file; an unreadable file is reported in `errors`.
Config load_config(const std::string& path, std::vector<std::string>& errors);

}  // namespace svc

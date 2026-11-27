#include <iostream>

#include "config.h"

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: svcconf FILE\n";
        return 2;
    }
    std::vector<std::string> errors;
    svc::Config cfg = svc::load_config(argv[1], errors);
    for (const auto& e : errors) std::cerr << e << "\n";
    std::cout << "host=" << cfg.host << " port=" << cfg.port
              << " timeout_ms=" << cfg.timeout_ms << " retries=" << cfg.retries << "\n";
    return errors.empty() ? 0 : 1;
}

#include "config.h"

#include <fstream>
#include <sstream>

#include "parse_util.h"

namespace svc {

namespace {

bool set_port(Config& cfg, const std::string& value) {
    long v = 0;
    if (!parse_number(value, v) || v < 1 || v > 65535) return false;
    cfg.port = static_cast<int>(v);
    return true;
}

bool set_timeout(Config& cfg, const std::string& value) {
    long seconds = 0;
    if (!parse_number(value, seconds) || seconds <= 0) return false;
    cfg.timeout_ms = seconds * 1000;
    return true;
}

bool set_retries(Config& cfg, const std::string& value) {
    long v = 0;
    if (!parse_number(value, v) || v < 0 || v > 10) return false;
    cfg.retries = static_cast<int>(v);
    return true;
}

bool set_host(Config& cfg, const std::string& value) {
    if (value.empty()) return false;
    cfg.host = value;
    return true;
}

}  // namespace

Config parse_config(const std::string& text, std::vector<std::string>& errors) {
    Config cfg;
    std::istringstream in(text);
    std::string line;
    int lineno = 0;
    while (std::getline(in, line)) {
        ++lineno;
        std::string t = trim(line);
        if (t.empty() || t[0] == '#') continue;
        auto eq = t.find('=');
        if (eq == std::string::npos) {
            errors.push_back("line " + std::to_string(lineno) + ": expected key = value");
            continue;
        }
        std::string key = trim(t.substr(0, eq));
        std::string value = trim(t.substr(eq + 1));
        bool ok;
        if (key == "host") ok = set_host(cfg, value);
        else if (key == "port") ok = set_port(cfg, value);
        else if (key == "timeout") ok = set_timeout(cfg, value);
        else if (key == "retries") ok = set_retries(cfg, value);
        else {
            errors.push_back("line " + std::to_string(lineno) + ": unknown key " + key);
            continue;
        }
        if (!ok) errors.push_back("line " + std::to_string(lineno) + ": invalid value for " + key);
    }
    return cfg;
}

Config load_config(const std::string& path, std::vector<std::string>& errors) {
    std::ifstream f(path);
    if (!f) {
        errors.push_back("cannot read " + path);
        return Config{};
    }
    std::stringstream buf;
    buf << f.rdbuf();
    return parse_config(buf.str(), errors);
}

}  // namespace svc

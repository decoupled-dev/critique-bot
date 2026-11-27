#include <iostream>
#include <string>
#include <vector>

#include "config.h"
#include "parse_util.h"

static int failures = 0;

#define CHECK(cond)                                                    \
    do {                                                               \
        if (!(cond)) {                                                 \
            std::cerr << "FAIL " << __LINE__ << ": " #cond "\n";       \
            ++failures;                                                \
        }                                                              \
    } while (0)

static svc::Config one(const std::string& text, size_t expect_errors) {
    std::vector<std::string> errs;
    svc::Config c = svc::parse_config(text, errs);
    if (errs.size() != expect_errors) {
        std::cerr << "FAIL: '" << text << "' gave " << errs.size() << " error(s), expected " << expect_errors << "\n";
        ++failures;
    }
    return c;
}

int main() {
    using namespace svc;
    // fractional timeouts
    CHECK(one("timeout = 1.5\n", 0).timeout_ms == 1500);
    CHECK(one("timeout = 0.25\n", 0).timeout_ms == 250);
    CHECK(one("timeout=0.1\n", 0).timeout_ms == 100);
    CHECK(one("timeout = 2\n", 0).timeout_ms == 2000);
    CHECK(one("timeout = 30\n", 0).timeout_ms == 30000);
    // still invalid
    CHECK(one("timeout = abc\n", 1).timeout_ms == 5000);
    CHECK(one("timeout = -1\n", 1).timeout_ms == 5000);
    CHECK(one("timeout = 0\n", 1).timeout_ms == 5000);
    CHECK(one("timeout = 1.5s\n", 1).timeout_ms == 5000);
    CHECK(one("timeout =\n", 1).timeout_ms == 5000);
    // other settings unchanged: whole numbers only
    CHECK(one("port = 80.5\n", 1).port == 8080);
    CHECK(one("port = 443\n", 0).port == 443);
    CHECK(one("port = 0\n", 1).port == 8080);
    CHECK(one("retries = 2.5\n", 1).retries == 3);
    CHECK(one("retries = 1.0\n", 1).retries == 3);
    CHECK(one("retries = 10\n", 0).retries == 10);
    {
        long v = 0;
        CHECK(parse_number("17", v) && v == 17);
        CHECK(!parse_number("1.0", v));
        CHECK(!parse_number("2.5", v));
        CHECK(!parse_number("", v));
    }
    {
        std::vector<std::string> errs;
        Config c = parse_config("# svc\nhost = 10.0.0.12\ntimeout = 1.5\nport = 9443\nretries = 2\n", errs);
        CHECK(errs.empty());
        CHECK(c.host == "10.0.0.12" && c.port == 9443 && c.timeout_ms == 1500 && c.retries == 2);
    }
    {
        std::vector<std::string> errs;
        parse_config("port = 1.5\ntimeout = x\n", errs);
        CHECK(errs.size() == 2 && errs[0] == "line 1: invalid value for port" &&
              errs[1] == "line 2: invalid value for timeout");
    }
    if (failures) {
        std::cerr << failures << " failure(s)\n";
        return 1;
    }
    std::cout << "hidden config tests passed\n";
    return 0;
}

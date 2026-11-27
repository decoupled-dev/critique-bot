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

int main() {
    using namespace svc;
    {
        std::vector<std::string> errs;
        Config c = parse_config("host = example\nport = 9000\ntimeout = 3\nretries = 0\n", errs);
        CHECK(errs.empty());
        CHECK(c.host == "example");
        CHECK(c.port == 9000);
        CHECK(c.timeout_ms == 3000);
        CHECK(c.retries == 0);
    }
    {
        std::vector<std::string> errs;
        Config c = parse_config("port = 80.5\n", errs);
        CHECK(errs.size() == 1);
        CHECK(c.port == 8080);
    }
    {
        std::vector<std::string> errs;
        Config c = parse_config("retries = 2.5\nport = 70000\n", errs);
        CHECK(errs.size() == 2);
        CHECK(c.retries == 3);
    }
    {
        std::vector<std::string> errs;
        parse_config("# comment\n\ncolour = red\n", errs);
        CHECK(errs.size() == 1 && errs[0] == "line 3: unknown key colour");
    }
    {
        long v = 0;
        CHECK(parse_number("42", v) && v == 42);
        CHECK(!parse_number("4x", v));
        CHECK(!parse_number("1.0", v));
    }
    if (failures) {
        std::cerr << failures << " failure(s)\n";
        return 1;
    }
    std::cout << "all tests passed\n";
    return 0;
}

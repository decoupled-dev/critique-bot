#pragma once

#include <string>

namespace svc {

// Strip spaces and tabs at both ends.
std::string trim(const std::string& s);

// Parse a whole number (optional leading '-'), the entire string must be used.
// Returns false and leaves `out` untouched on any error, including overflow.
bool parse_number(const std::string& s, long& out);

// Parse a finite decimal number such as "1.5" or "2"; the entire string must be used.
bool parse_decimal(const std::string& s, double& out);

}  // namespace svc

#pragma once
#include <vector>
#include "AudioZone.h"

namespace car {

class ZoneRouter {
public:
    explicit ZoneRouter(std::vector<AudioZone> a7);
    AudioZone* doIt1(int a6);
    size_t count() const { return zones_.size(); }

private:
    std::vector<AudioZone> zones_;
};

}  // namespace car

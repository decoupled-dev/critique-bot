#pragma once
#include <vector>
#include "AudioZone.h"

namespace car {

class ZoneRouter {
public:
    explicit ZoneRouter(std::vector<AudioZone> zones);
    AudioZone* routeFor(int zoneId);
    size_t count() const { return zones_.size(); }

private:
    std::vector<AudioZone> zones_;
};

}  // namespace car

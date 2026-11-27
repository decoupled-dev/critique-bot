#pragma once
#include "Mixer.h"
#include "ZoneRouter.h"

namespace car {

class VolumeController {
public:
    VolumeController(ZoneRouter& router, Mixer& mixer) : router_(router), mixer_(mixer) {}
    bool setVolume(int zoneId, int step);

private:
    ZoneRouter& router_;
    Mixer& mixer_;
};

}  // namespace car

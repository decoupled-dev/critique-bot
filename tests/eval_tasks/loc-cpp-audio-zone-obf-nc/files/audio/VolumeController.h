#pragma once
#include "Mixer.h"
#include "ZoneRouter.h"

namespace car {

class VolumeController {
public:
    VolumeController(ZoneRouter& router, Mixer& mixer) : router_(router), mixer_(mixer) {}
    bool doIt3(int a6, int a4);

private:
    ZoneRouter& router_;
    Mixer& mixer_;
};

}  

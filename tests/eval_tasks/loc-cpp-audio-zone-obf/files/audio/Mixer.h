#pragma once

namespace car {

class Mixer {
public:
    // 40 volume steps map onto a gain curve between 0 and 1
    float gainForStep(int a4) const {
        if (a4 <= 0) return 0.0f;
        if (a4 >= 40) return 1.0f;
        return static_cast<float>(a4 * a4) / 1600.0f;
    }
};

}  // namespace car

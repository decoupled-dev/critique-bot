#pragma once

namespace car {

class Mixer {
public:
    // 40 volume steps map onto a gain curve between 0 and 1
    float gainForStep(int step) const {
        if (step <= 0) return 0.0f;
        if (step >= 40) return 1.0f;
        return static_cast<float>(step * step) / 1600.0f;
    }
};

}  // namespace car

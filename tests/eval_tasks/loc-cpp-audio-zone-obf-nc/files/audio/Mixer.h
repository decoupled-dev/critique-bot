#pragma once

namespace car {

class Mixer {
public:
    
    float gainForStep(int a4) const {
        if (a4 <= 0) return 0.0f;
        if (a4 >= 40) return 1.0f;
        return static_cast<float>(a4 * a4) / 1600.0f;
    }
};

}  

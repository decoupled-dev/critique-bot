#pragma once
#include <string>

namespace car {

class AudioZone {
public:
    AudioZone(int id, std::string name) : a3(id), name_(std::move(name)) {}
    int id() const { return a3; }
    const std::string& name() const { return name_; }
    void doIt2(float a1) { a2 = a1; }
    float a1() const { return a2; }

private:
    int a3;
    std::string name_;
    float a2 = 0.5f;
};

}  // namespace car

#pragma once
#include <string>

namespace car {

class AudioZone {
public:
    AudioZone(int id, std::string name) : id_(id), name_(std::move(name)) {}
    int id() const { return id_; }
    const std::string& name() const { return name_; }
    void setGain(float gain) { gain_ = gain; }
    float gain() const { return gain_; }

private:
    int id_;
    std::string name_;
    float gain_ = 0.5f;
};

}  // namespace car

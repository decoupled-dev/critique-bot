#ifndef FAN_H
#define FAN_H

#define FAN_MAX 9

struct fan_state {
    int level;
    int duty;
};

int fan_set(struct fan_state *s, int level);
int pwm_duty_for_level(int level);
int thermal_policy(int celsius);

#endif

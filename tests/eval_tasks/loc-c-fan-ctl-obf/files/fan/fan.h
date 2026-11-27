#ifndef FAN_H
#define FAN_H

#define FAN_MAX 9

struct mgr1 {
    int a5;
    int a4;
};

int doIt1(struct mgr1 *s, int a5);
int doIt2(int a5);
int doIt3(int a3);

#endif

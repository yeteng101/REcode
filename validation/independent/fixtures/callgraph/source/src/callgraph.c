#include <stddef.h>

#define APPLY_ONCE(fn, value) ((fn)(value))

static int leaf(int value) {
    return value + 1;
}

static int bridge(int value) {
    return leaf(value);
}

static int doubled(int value) {
    return value * 2;
}

static int unused(int value) {
    return value - 1;
}

int entry(int value) {
    int first = bridge(value);
    int second = APPLY_ONCE(doubled, first);
    return second;
}

int negative_control(int value) {
    return value;
}

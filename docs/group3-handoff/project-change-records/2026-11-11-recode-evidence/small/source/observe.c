#include "state.h"
int observe(int value) {
    allocate_temporary();
    return value + 1;
}

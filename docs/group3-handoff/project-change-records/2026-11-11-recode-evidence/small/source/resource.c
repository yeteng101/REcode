#include <stdlib.h>
void allocate_temporary(void) {
    char *buffer = malloc(32);
    free(buffer);
}

#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>

static char *create_record(void) {
    char *buffer = malloc(32);
    return buffer;
}

static void destroy_record(char *buffer) {
    free(buffer);
}

static void open_and_close(const char *path) {
    int fd = open(path, O_RDONLY);
    if (fd >= 0) {
        close(fd);
    }
}

void transfer_and_release(void) {
    char *owner = create_record();
    char *alias = owner;
    destroy_record(alias);
}

void leak_on_visible_path(void) {
    char *buffer = malloc(8);
    buffer[0] = '\0';
}

int main(void) {
    transfer_and_release();
    leak_on_visible_path();
    open_and_close("fixture.txt");
    return 0;
}

#include <pthread.h>
#include "state.h"
int counter = 0;
pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
void *writer_a(void *unused) {
    (void)unused;
    pthread_mutex_lock(&mutex);
    counter += 2;
    pthread_mutex_unlock(&mutex);
    return 0;
}
void *writer_b(void *unused) {
    (void)unused;
    pthread_mutex_lock(&mutex);
    counter++;
    pthread_mutex_unlock(&mutex);
    return 0;
}
int main(void) {
    pthread_t a, b;
    pthread_create(&a, 0, writer_a, 0);
    pthread_create(&b, 0, writer_b, 0);
    pthread_join(a, 0);
    pthread_join(b, 0);
    return observe(counter);
}

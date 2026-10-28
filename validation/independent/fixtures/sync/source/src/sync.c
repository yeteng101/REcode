#include <pthread.h>
#include <stdatomic.h>

static pthread_mutex_t guard = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready = PTHREAD_COND_INITIALIZER;
static pthread_t worker_thread;
static int state = 0;
static _Atomic int mailbox = 0;
static _Atomic int counter = 0;

static void *worker(void *arg) {
    pthread_mutex_lock(&guard);
    state = 1;
    pthread_cond_signal(&ready);
    atomic_store_explicit(&mailbox, 1, memory_order_release);
    pthread_mutex_unlock(&guard);
    return arg;
}

void start_worker(void) {
    pthread_create(&worker_thread, NULL, worker, NULL);
    pthread_join(worker_thread, NULL);
}

void wait_worker(void) {
    pthread_mutex_lock(&guard);
    while (state == 0) {
        pthread_cond_wait(&ready, &guard);
    }
    pthread_mutex_unlock(&guard);
}

int observe_mailbox(void) {
    return atomic_load_explicit(&mailbox, memory_order_acquire);
}

void relaxed_publish(void) {
    atomic_store_explicit(&counter, 1, memory_order_relaxed);
}

int relaxed_observe(void) {
    return atomic_load_explicit(&counter, memory_order_relaxed);
}

int main(void) {
    start_worker();
    wait_worker();
    observe_mailbox();
    relaxed_publish();
    return relaxed_observe();
}

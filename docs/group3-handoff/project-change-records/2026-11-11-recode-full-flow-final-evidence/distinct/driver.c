#define _GNU_SOURCE
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/* Compile the actual product callbacks and observer with their own semantics. */
#define main source_program_main
#include "workers.c"
#undef main
#include "observe.c"
#include "resource.c"

enum {
    BATCH_COUNT = 128,
    CALLS_PER_BATCH = 16384,
    CALLS_PER_WRITER = BATCH_COUNT * CALLS_PER_BATCH
};

/* This barrier belongs exclusively to the harness. */
static pthread_barrier_t batch_boundary;

struct worker_plan {
    void *(*callback)(void *);
};

static void timeout_handler(int signal_number)
{
    (void)signal_number;
    _Exit(90);
}

static void harness_failure(const char *operation)
{
    fprintf(stderr, "test harness failure in %s\n", operation);
    _Exit(90);
}

static void synchronize_batch(void)
{
    int result = pthread_barrier_wait(&batch_boundary);
    if (result != 0 && result != PTHREAD_BARRIER_SERIAL_THREAD)
        harness_failure("pthread_barrier_wait");
}

static void *exercise_source_callback(void *argument)
{
    const struct worker_plan *plan = argument;

    for (int batch = 0; batch < BATCH_COUNT; ++batch) {
        synchronize_batch();
        for (int call = 0; call < CALLS_PER_BATCH; ++call)
            plan->callback(NULL);
        synchronize_batch();
    }
    return NULL;
}

int main(void)
{
    struct sigaction action = {0};
    action.sa_handler = timeout_handler;
    if (sigemptyset(&action.sa_mask) != 0 ||
        sigaction(SIGALRM, &action, NULL) != 0)
        harness_failure("watchdog setup");
    alarm(8);

    if (pthread_barrier_init(&batch_boundary, NULL, 2) != 0)
        harness_failure("pthread_barrier_init");

    /* Both writers are quiescent during this reset and after both joins. */
    counter = 0;
    const struct worker_plan plans[2] = {{writer_a}, {writer_b}};
    pthread_t threads[2];
    if (pthread_create(&threads[0], NULL, exercise_source_callback,
                       (void *)&plans[0]) != 0)
        harness_failure("first pthread_create");
    if (pthread_create(&threads[1], NULL, exercise_source_callback,
                       (void *)&plans[1]) != 0)
        harness_failure("second pthread_create");
    if (pthread_join(threads[0], NULL) != 0 ||
        pthread_join(threads[1], NULL) != 0)
        harness_failure("pthread_join");

    if (pthread_barrier_destroy(&batch_boundary) != 0)
        harness_failure("pthread_barrier_destroy");

    /* Source writer_a contributes two and writer_b contributes one per call.
     * The actual source observer adds one to the completed counter value. */
    int observed = observe(counter);
    const int expected = 3 * CALLS_PER_WRITER + 1;
    alarm(0);
    if (observed != expected) {
        fprintf(stderr, "completed writer arithmetic lost updates: got %d, expected %d\n",
                observed, expected);
        return 42;
    }
    return 0;
}

#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

/* Compile the actual product implementations with their advertised include root. */
#define main product_main
#include "/source/workers.c"
#undef main
#include "/source/observe.c"
#include "/source/resource.c"

enum { BATCHES = 1024, CALLS_PER_BATCH = 4096 };

static pthread_barrier_t batch_start;
static pthread_barrier_t batch_done;

/* Volatile function pointers keep every iteration an actual source callback call. */
static void *(*volatile callbacks[2])(void *) = { writer_a, writer_b };

struct worker_parameters {
    int callback_index;
    int cpu;
};

static void harness_timeout(int signal_number)
{
    (void)signal_number;
    _Exit(90);
}

static void wait_at(pthread_barrier_t *barrier)
{
    int result = pthread_barrier_wait(barrier);
    if (result != 0 && result != PTHREAD_BARRIER_SERIAL_THREAD) {
        _Exit(90);
    }
}

static void *run_batches(void *argument)
{
    struct worker_parameters *parameters = argument;
    cpu_set_t selected;
    CPU_ZERO(&selected);
    CPU_SET(parameters->cpu, &selected);
    if (pthread_setaffinity_np(pthread_self(), sizeof(selected), &selected) != 0) {
        _Exit(90);
    }

    for (int batch = 0; batch < BATCHES; ++batch) {
        wait_at(&batch_start);
        for (int call = 0; call < CALLS_PER_BATCH; ++call) {
            callbacks[parameters->callback_index](NULL);
        }
        wait_at(&batch_done);
    }
    return NULL;
}

int main(void)
{
    struct sigaction action = {0};
    action.sa_handler = harness_timeout;
    if (sigemptyset(&action.sa_mask) != 0 ||
        sigaction(SIGALRM, &action, NULL) != 0) {
        return 90;
    }
    alarm(8);

    cpu_set_t available;
    if (sched_getaffinity(0, sizeof(available), &available) != 0) {
        return 90;
    }
    struct worker_parameters parameters[2] = {{0, -1}, {1, -1}};
    int selected_count = 0;
    for (int cpu = 0; cpu < CPU_SETSIZE && selected_count < 2; ++cpu) {
        if (CPU_ISSET(cpu, &available)) {
            parameters[selected_count++].cpu = cpu;
        }
    }
    if (selected_count != 2) {
        return 90;
    }

    if (pthread_barrier_init(&batch_start, NULL, 3) != 0 ||
        pthread_barrier_init(&batch_done, NULL, 3) != 0) {
        return 90;
    }

    pthread_t threads[2];
    if (pthread_create(&threads[0], NULL, run_batches, &parameters[0]) != 0 ||
        pthread_create(&threads[1], NULL, run_batches, &parameters[1]) != 0) {
        _Exit(90);
    }

    for (int batch = 0; batch < BATCHES; ++batch) {
        wait_at(&batch_start);
        wait_at(&batch_done);

        /* Both workers now wait at the next start barrier, or have returned. */
        int expected = 3 * CALLS_PER_BATCH * (batch + 1);
        int observed = observe(counter);
        if (counter != expected || observed != expected + 1) {
            fprintf(stderr,
                    "lost arithmetic update after batch %d: counter=%d expected=%d observe=%d expected=%d\n",
                    batch + 1, counter, expected, observed, expected + 1);
            _Exit(42);
        }
    }

    if (pthread_join(threads[0], NULL) != 0 ||
        pthread_join(threads[1], NULL) != 0) {
        return 90;
    }
    if (pthread_barrier_destroy(&batch_start) != 0 ||
        pthread_barrier_destroy(&batch_done) != 0) {
        return 90;
    }
    alarm(0);
    return 0;
}

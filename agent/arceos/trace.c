#include "trace.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static const char trace_stdout_prefix[] = "__SYZABI_TRACE_EVENT__ ";

static unsigned long long monotonic_ns(void)
{
    struct timespec ts;

    memset(&ts, 0, sizeof(ts));
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    return (unsigned long long)ts.tv_sec * 1000000000ULL + (unsigned long long)ts.tv_nsec;
}

static void emit_event(const char* syscall_name, long syscall_number, long call_index,
                       long a0, long a1, long a2, long a3, long a4, long a5,
                       long ret, int err, unsigned long long start_ns, unsigned long long end_ns)
{
    printf(
        "%s{\"args\":[%ld,%ld,%ld,%ld,%ld,%ld],\"end_ns\":%llu,\"errno\":%d,\"event_index\":%ld,"
        "\"outputs\":[],\"return_value\":%ld,\"side\":\"candidate\",\"start_ns\":%llu,"
        "\"syscall_name\":\"%s\",\"syscall_number\":%ld}\n",
        trace_stdout_prefix,
        a0,
        a1,
        a2,
        a3,
        a4,
        a5,
        end_ns,
        err,
        call_index,
        ret,
        start_ns,
        syscall_name,
        syscall_number
    );
    fflush(stdout);
}

static long dispatch_call(const char* syscall_name, long a0, long a1, long a2)
{
    if (strcmp(syscall_name, "close") == 0)
        return close((int)a0);
    if (strcmp(syscall_name, "read") == 0)
        return read((int)a0, (void*)a1, (size_t)a2);
    if (strcmp(syscall_name, "write") == 0)
        return write((int)a0, (const void*)a1, (size_t)a2);
    if (strcmp(syscall_name, "open") == 0) {
        if ((int)a1 & O_CREAT)
            return open((const char*)a0, (int)a1, (int)a2);
        return open((const char*)a0, (int)a1);
    }
    if (strcmp(syscall_name, "openat") == 0) {
        if ((int)a0 != AT_FDCWD) {
            errno = ENOSYS;
            return -1;
        }
        if ((int)a2 & O_CREAT)
            return open((const char*)a1, (int)a2, 0);
        return open((const char*)a1, (int)a2);
    }

    errno = ENOSYS;
    return -1;
}

long traced_syscall(const char* syscall_name, long syscall_number, long call_index,
                    long a0, long a1, long a2, long a3, long a4, long a5)
{
    unsigned long long start_ns = monotonic_ns();
    unsigned long long end_ns;
    long ret;
    int err;
    int exit_code;

    if (strcmp(syscall_name, "exit") == 0 || strcmp(syscall_name, "exit_group") == 0) {
        end_ns = monotonic_ns();
        emit_event(syscall_name, syscall_number, call_index, a0, a1, a2, a3, a4, a5, 0, 0, start_ns, end_ns);
        exit_code = (int)a0;
        exit(exit_code);
    }

    if (strcmp(syscall_name, "close") == 0 && ((int)a0 == STDOUT_FILENO || (int)a0 == STDERR_FILENO)) {
        end_ns = monotonic_ns();
        emit_event(syscall_name, syscall_number, call_index, a0, a1, a2, a3, a4, a5, 0, 0, start_ns, end_ns);
        return 0;
    }

    errno = 0;
    ret = dispatch_call(syscall_name, a0, a1, a2);
    err = ret < 0 ? errno : 0;
    end_ns = monotonic_ns();
    emit_event(syscall_name, syscall_number, call_index, a0, a1, a2, a3, a4, a5, ret, err, start_ns, end_ns);
    return ret;
}

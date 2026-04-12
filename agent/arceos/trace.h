#ifndef SYZABI_ARCEOS_TRACE_H
#define SYZABI_ARCEOS_TRACE_H

long traced_syscall(const char* syscall_name, long syscall_number, long call_index,
                    long a0, long a1, long a2, long a3, long a4, long a5);

#endif

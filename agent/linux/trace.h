#ifndef SYZABI_TRACE_H
#define SYZABI_TRACE_H

#include <stddef.h>

struct syzabi_injection {
	int enabled;
	long call_index;
	const char* syscall_name;
	const char* field;
	long value;
};

const char* syzabi_side(void);
const char* syzabi_events_path(void);
size_t syzabi_preview_bytes(void);
struct syzabi_injection syzabi_get_injection(void);

long traced_syscall(const char* syscall_name, long syscall_number, long call_index,
		    long a0, long a1, long a2, long a3, long a4, long a5);

#endif

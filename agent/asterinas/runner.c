#include "../linux/trace.h"

#include <stddef.h>
#include <stdlib.h>
#include <string.h>

const char* syzabi_side(void)
{
	const char* value = getenv("SYZABI_SIDE");
	return value ? value : "candidate";
}

const char* syzabi_events_path(void)
{
	const char* value = getenv("SYZABI_TRACE_EVENTS_PATH");
	return value ? value : "/tmp/syzabi/raw-trace.events.jsonl";
}

size_t syzabi_preview_bytes(void)
{
	const char* value = getenv("SYZABI_TRACE_PREVIEW_BYTES");
	if (!value || !*value)
		return 32;
	char* end = NULL;
	unsigned long parsed = strtoul(value, &end, 10);
	if (!end || *end != '\0' || parsed == 0)
		return 32;
	return (size_t)parsed;
}

struct syzabi_injection syzabi_get_injection(void)
{
	struct syzabi_injection injection = {
		.enabled = 0,
		.call_index = -1,
		.syscall_name = 0,
		.field = 0,
		.value = 0,
	};
	const char* enabled = getenv("SYZABI_INJECT_TRACE_ENABLED");
	if (!enabled || strcmp(enabled, "1") != 0)
		return injection;
	injection.enabled = 1;
	const char* index = getenv("SYZABI_INJECT_TRACE_CALL_INDEX");
	if (index && *index) {
		char* end = NULL;
		injection.call_index = strtol(index, &end, 10);
		if (!end || *end != '\0')
			injection.call_index = -1;
	}
	injection.syscall_name = getenv("SYZABI_INJECT_TRACE_SYSCALL");
	injection.field = getenv("SYZABI_INJECT_TRACE_FIELD");
	const char* value = getenv("SYZABI_INJECT_TRACE_VALUE");
	if (value && *value) {
		char* end = NULL;
		injection.value = strtol(value, &end, 10);
		if (!end || *end != '\0')
			injection.value = 0;
	}
	return injection;
}

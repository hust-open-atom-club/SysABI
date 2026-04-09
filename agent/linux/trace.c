#include "trace.h"

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

typedef unsigned char uint8;
typedef unsigned int uint32;
typedef unsigned long long uint64;

typedef struct {
	uint8 data[64];
	uint32 datalen;
	uint64 bitlen;
	uint32 state[8];
} sha256_ctx;

struct output_probe {
	const char* label;
	int arg_index;
	const unsigned char* ptr;
	size_t length;
	int enabled;
	int resource_kind_fd;
	long resource_values[2];
	size_t resource_count;
};

static int trace_fd = -1;

static uint32 rotr(uint32 value, uint32 bits)
{
	return (value >> bits) | (value << (32 - bits));
}

static void sha256_transform(sha256_ctx* ctx, const uint8 data[])
{
	static const uint32 k[64] = {
		0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
		0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
		0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
		0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
		0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
		0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
		0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
		0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
		0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
		0xc67178f2,
	};
	uint32 a, b, c, d, e, f, g, h, i, j, m[64], s0, s1, ch, temp1, temp2, maj, S0, S1;

	for (i = 0, j = 0; i < 16; ++i, j += 4)
		m[i] = (data[j] << 24) | (data[j + 1] << 16) | (data[j + 2] << 8) | (data[j + 3]);
	for (; i < 64; ++i) {
		s0 = rotr(m[i - 15], 7) ^ rotr(m[i - 15], 18) ^ (m[i - 15] >> 3);
		s1 = rotr(m[i - 2], 17) ^ rotr(m[i - 2], 19) ^ (m[i - 2] >> 10);
		m[i] = m[i - 16] + s0 + m[i - 7] + s1;
	}
	a = ctx->state[0];
	b = ctx->state[1];
	c = ctx->state[2];
	d = ctx->state[3];
	e = ctx->state[4];
	f = ctx->state[5];
	g = ctx->state[6];
	h = ctx->state[7];
	for (i = 0; i < 64; ++i) {
		S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
		ch = (e & f) ^ (~e & g);
		temp1 = h + S1 + ch + k[i] + m[i];
		S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
		maj = (a & b) ^ (a & c) ^ (b & c);
		temp2 = S0 + maj;
		h = g;
		g = f;
		f = e;
		e = d + temp1;
		d = c;
		c = b;
		b = a;
		a = temp1 + temp2;
	}
	ctx->state[0] += a;
	ctx->state[1] += b;
	ctx->state[2] += c;
	ctx->state[3] += d;
	ctx->state[4] += e;
	ctx->state[5] += f;
	ctx->state[6] += g;
	ctx->state[7] += h;
}

static void sha256_init(sha256_ctx* ctx)
{
	ctx->datalen = 0;
	ctx->bitlen = 0;
	ctx->state[0] = 0x6a09e667;
	ctx->state[1] = 0xbb67ae85;
	ctx->state[2] = 0x3c6ef372;
	ctx->state[3] = 0xa54ff53a;
	ctx->state[4] = 0x510e527f;
	ctx->state[5] = 0x9b05688c;
	ctx->state[6] = 0x1f83d9ab;
	ctx->state[7] = 0x5be0cd19;
}

static void sha256_update(sha256_ctx* ctx, const uint8 data[], size_t len)
{
	size_t i;
	for (i = 0; i < len; ++i) {
		ctx->data[ctx->datalen] = data[i];
		ctx->datalen++;
		if (ctx->datalen == 64) {
			sha256_transform(ctx, ctx->data);
			ctx->bitlen += 512;
			ctx->datalen = 0;
		}
	}
}

static void sha256_final(sha256_ctx* ctx, uint8 hash[])
{
	uint32 i = ctx->datalen;

	if (ctx->datalen < 56) {
		ctx->data[i++] = 0x80;
		while (i < 56)
			ctx->data[i++] = 0x00;
	} else {
		ctx->data[i++] = 0x80;
		while (i < 64)
			ctx->data[i++] = 0x00;
		sha256_transform(ctx, ctx->data);
		memset(ctx->data, 0, 56);
	}

	ctx->bitlen += ctx->datalen * 8;
	ctx->data[63] = ctx->bitlen;
	ctx->data[62] = ctx->bitlen >> 8;
	ctx->data[61] = ctx->bitlen >> 16;
	ctx->data[60] = ctx->bitlen >> 24;
	ctx->data[59] = ctx->bitlen >> 32;
	ctx->data[58] = ctx->bitlen >> 40;
	ctx->data[57] = ctx->bitlen >> 48;
	ctx->data[56] = ctx->bitlen >> 56;
	sha256_transform(ctx, ctx->data);

	for (i = 0; i < 4; ++i) {
		hash[i] = (ctx->state[0] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 4] = (ctx->state[1] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 8] = (ctx->state[2] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 12] = (ctx->state[3] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 16] = (ctx->state[4] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 20] = (ctx->state[5] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 24] = (ctx->state[6] >> (24 - i * 8)) & 0x000000ff;
		hash[i + 28] = (ctx->state[7] >> (24 - i * 8)) & 0x000000ff;
	}
}

static uint64 monotonic_ns(void)
{
	struct timespec ts;
	memset(&ts, 0, sizeof(ts));
#ifdef __NR_clock_gettime
	if (syscall(__NR_clock_gettime, CLOCK_MONOTONIC, &ts) != 0)
		return 0;
#else
	return 0;
#endif
	return (uint64)ts.tv_sec * 1000000000ULL + (uint64)ts.tv_nsec;
}

static int open_trace_file(void)
{
	if (trace_fd >= 0)
		return trace_fd;
	const char* path = syzabi_events_path();
	if (!path || !*path)
		return -1;
	trace_fd = (int)syscall(__NR_openat, AT_FDCWD, path, O_WRONLY | O_CREAT | O_APPEND, 0644);
	if (trace_fd < 0)
		return -1;
	return trace_fd;
}

static size_t safe_probe_length(size_t requested)
{
	size_t preview = syzabi_preview_bytes();
	if (requested == 0)
		return 0;
	return requested < preview ? requested : preview;
}

static void hex_preview(const unsigned char* ptr, size_t len, char* out, size_t out_size)
{
	static const char hex[] = "0123456789abcdef";
	size_t i, used = 0;
	size_t max_len;
	if (!ptr || !len || out_size == 0) {
		if (out_size)
			out[0] = '\0';
		return;
	}
	max_len = (out_size - 1) / 2;
	if (len > max_len)
		len = max_len;
	for (i = 0; i < len; ++i) {
		out[used++] = hex[(ptr[i] >> 4) & 0x0f];
		out[used++] = hex[ptr[i] & 0x0f];
	}
	out[used] = '\0';
}

static void digest_hex(const unsigned char* ptr, size_t len, char* out, size_t out_size)
{
	sha256_ctx ctx;
	unsigned char hash[32];
	static const char hex[] = "0123456789abcdef";
	size_t i, used = 0;
	if (out_size == 0)
		return;
	sha256_init(&ctx);
	sha256_update(&ctx, ptr, len);
	sha256_final(&ctx, hash);
	for (i = 0; i < sizeof(hash) && used + 2 < out_size; ++i) {
		out[used++] = hex[(hash[i] >> 4) & 0x0f];
		out[used++] = hex[hash[i] & 0x0f];
	}
	out[used] = '\0';
}

static size_t min_size(size_t a, size_t b)
{
	return a < b ? a : b;
}

static void zero_range(unsigned char* ptr, size_t len, size_t offset, size_t size)
{
	if (offset >= len)
		return;
	if (size > len - offset)
		size = len - offset;
	memset(ptr + offset, 0, size);
}

static void sanitize_stat_bytes(const unsigned char* ptr, size_t len, unsigned char* out)
{
	memcpy(out, ptr, len);
	if (len < sizeof(struct stat))
		return;
	zero_range(out, len, offsetof(struct stat, st_dev), sizeof(((struct stat*)0)->st_dev));
	zero_range(out, len, offsetof(struct stat, st_ino), sizeof(((struct stat*)0)->st_ino));
	zero_range(out, len, offsetof(struct stat, st_atim), sizeof(((struct stat*)0)->st_atim));
	zero_range(out, len, offsetof(struct stat, st_mtim), sizeof(((struct stat*)0)->st_mtim));
	zero_range(out, len, offsetof(struct stat, st_ctim), sizeof(((struct stat*)0)->st_ctim));
}

static int is_stat_probe(const struct output_probe* probe)
{
	return probe->label && strcmp(probe->label, "stat") == 0 && probe->length == sizeof(struct stat);
}

static void probe_preview_and_digest(const struct output_probe* probe, char* preview, size_t preview_size, char* digest,
				     size_t digest_size)
{
	size_t preview_len = safe_probe_length(probe->length);
	if (is_stat_probe(probe)) {
		unsigned char sanitized[sizeof(struct stat)];
		sanitize_stat_bytes(probe->ptr, probe->length, sanitized);
		hex_preview(sanitized, preview_len, preview, preview_size);
		digest_hex(sanitized, probe->length, digest, digest_size);
		return;
	}
	hex_preview(probe->ptr, preview_len, preview, preview_size);
	digest_hex(probe->ptr, probe->length, digest, digest_size);
}

static int is_injection_match(struct syzabi_injection injection, long call_index, const char* syscall_name)
{
	if (!injection.enabled)
		return 0;
	if (injection.call_index >= 0 && injection.call_index != call_index)
		return 0;
	if (injection.syscall_name && *injection.syscall_name &&
	    strcmp(injection.syscall_name, syscall_name) != 0)
		return 0;
	return 1;
}

static size_t append_raw(char* out, size_t out_size, size_t used, const char* src, size_t len)
{
	size_t max;
	size_t room;
	size_t copy;

	if (out_size == 0)
		return used;
	max = out_size - 1;
	if (used > max)
		used = max;
	room = max - used;
	copy = len < room ? len : room;
	if (copy != 0)
		memcpy(out + used, src, copy);
	used += copy;
	out[used] = '\0';
	return used;
}

static size_t append_cstr(char* out, size_t out_size, size_t used, const char* src)
{
	return append_raw(out, out_size, used, src, strlen(src));
}

static size_t append_char(char* out, size_t out_size, size_t used, char value)
{
	return append_raw(out, out_size, used, &value, 1);
}

static size_t append_u64(char* out, size_t out_size, size_t used, uint64 value)
{
	char digits[32];
	size_t count = 0;
	do {
		digits[count++] = (char)('0' + (value % 10));
		value /= 10;
	} while (value != 0);
	while (count != 0)
		used = append_char(out, out_size, used, digits[--count]);
	return used;
}

static size_t append_i64(char* out, size_t out_size, size_t used, long value)
{
	uint64 magnitude;

	if (value < 0) {
		used = append_char(out, out_size, used, '-');
		magnitude = (uint64)(-(value + 1)) + 1;
	} else {
		magnitude = (uint64)value;
	}
	return append_u64(out, out_size, used, magnitude);
}

static size_t append_json_string(char* out, size_t out_size, size_t used, const char* src)
{
	const unsigned char* cursor = (const unsigned char*)src;

	used = append_char(out, out_size, used, '"');
	if (!src)
		return append_char(out, out_size, used, '"');
	while (*cursor != '\0') {
		switch (*cursor) {
		case '\\':
			used = append_cstr(out, out_size, used, "\\\\");
			break;
		case '"':
			used = append_cstr(out, out_size, used, "\\\"");
			break;
		case '\n':
			used = append_cstr(out, out_size, used, "\\n");
			break;
		case '\r':
			used = append_cstr(out, out_size, used, "\\r");
			break;
		case '\t':
			used = append_cstr(out, out_size, used, "\\t");
			break;
		default:
			if (*cursor < 0x20) {
				used = append_char(out, out_size, used, '?');
			} else {
				used = append_char(out, out_size, used, (char)*cursor);
			}
			break;
		}
		cursor++;
	}
	return append_char(out, out_size, used, '"');
}

static void emit_trace_line(const char* line, size_t len)
{
	int fd = open_trace_file();
	size_t written = 0;

	if (fd < 0)
		return;
	while (written < len) {
		long ret = syscall(__NR_write, fd, line + written, len - written);
		if (ret <= 0)
			return;
		written += (size_t)ret;
	}
}

static size_t collect_outputs(long syscall_number, long ret, long a0, long a1, long a2, long a3,
			      struct output_probe probes[3])
{
	size_t count = 0;
	memset(probes, 0, sizeof(struct output_probe) * 3);
	if (ret < 0)
		return 0;
	switch (syscall_number) {
	case __NR_read:
	case __NR_pread64:
		if (a1 != 0 && ret > 0) {
			probes[count].label = "buf";
			probes[count].arg_index = 1;
			probes[count].ptr = (const unsigned char*)(uintptr_t)a1;
			probes[count].length = min_size((size_t)ret, 4096);
			probes[count].enabled = 1;
			count++;
		}
		break;
	case __NR_pipe:
#ifdef __NR_pipe2
	case __NR_pipe2:
#endif
		if (a0 != 0) {
			const int* pipefd = (const int*)(uintptr_t)a0;
			probes[count].label = "pipefd";
			probes[count].arg_index = 0;
			probes[count].ptr = (const unsigned char*)pipefd;
			probes[count].length = sizeof(int) * 2;
			probes[count].enabled = 1;
			probes[count].resource_kind_fd = 1;
			probes[count].resource_count = 2;
			probes[count].resource_values[0] = pipefd[0];
			probes[count].resource_values[1] = pipefd[1];
			count++;
		}
		break;
#ifdef __NR_socketpair
	case __NR_socketpair:
		if (a3 != 0) {
			const int* sv = (const int*)(uintptr_t)a3;
			probes[count].label = "socketpair";
			probes[count].arg_index = 3;
			probes[count].ptr = (const unsigned char*)sv;
			probes[count].length = sizeof(int) * 2;
			probes[count].enabled = 1;
			probes[count].resource_kind_fd = 1;
			probes[count].resource_count = 2;
			probes[count].resource_values[0] = sv[0];
			probes[count].resource_values[1] = sv[1];
			count++;
		}
		break;
#endif
	case __NR_fstat:
		if (a1 != 0) {
			probes[count].label = "stat";
			probes[count].arg_index = 1;
			probes[count].ptr = (const unsigned char*)(uintptr_t)a1;
			probes[count].length = sizeof(struct stat);
			probes[count].enabled = 1;
			count++;
		}
		break;
	case __NR_newfstatat:
		if (a2 != 0) {
			probes[count].label = "stat";
			probes[count].arg_index = 2;
			probes[count].ptr = (const unsigned char*)(uintptr_t)a2;
			probes[count].length = sizeof(struct stat);
			probes[count].enabled = 1;
			count++;
		}
		break;
	case __NR_wait4:
		if (a1 != 0) {
			probes[count].label = "status";
			probes[count].arg_index = 1;
			probes[count].ptr = (const unsigned char*)(uintptr_t)a1;
			probes[count].length = sizeof(int);
			probes[count].enabled = 1;
			count++;
		}
		if (a3 != 0) {
			probes[count].label = "rusage";
			probes[count].arg_index = 3;
			probes[count].ptr = (const unsigned char*)(uintptr_t)a3;
			probes[count].length = sizeof(struct rusage);
			probes[count].enabled = 1;
			count++;
		}
		break;
	}
	return count;
}

static void emit_event(const char* syscall_name, long syscall_number, long call_index, long args[6],
		       long traced_ret, int traced_errno, uint64 start_ns, uint64 end_ns,
		       struct output_probe probes[3], size_t probe_count)
{
	char line[32768];
	size_t i;
	size_t used = 0;

	used = append_cstr(line, sizeof(line), used, "{\"event_index\":");
	used = append_i64(line, sizeof(line), used, call_index);
	used = append_cstr(line, sizeof(line), used, ",\"side\":");
	used = append_json_string(line, sizeof(line), used, syzabi_side());
	used = append_cstr(line, sizeof(line), used, ",\"syscall_name\":");
	used = append_json_string(line, sizeof(line), used, syscall_name);
	used = append_cstr(line, sizeof(line), used, ",\"syscall_number\":");
	used = append_i64(line, sizeof(line), used, syscall_number);
	used = append_cstr(line, sizeof(line), used, ",\"args\":[");
	for (i = 0; i < 6; ++i) {
		if (i != 0)
			used = append_char(line, sizeof(line), used, ',');
		used = append_i64(line, sizeof(line), used, args[i]);
	}
	used = append_cstr(line, sizeof(line), used, "],\"return_value\":");
	used = append_i64(line, sizeof(line), used, traced_ret);
	used = append_cstr(line, sizeof(line), used, ",\"errno\":");
	used = append_i64(line, sizeof(line), used, traced_errno);
	used = append_cstr(line, sizeof(line), used, ",\"start_ns\":");
	used = append_u64(line, sizeof(line), used, start_ns);
	used = append_cstr(line, sizeof(line), used, ",\"end_ns\":");
	used = append_u64(line, sizeof(line), used, end_ns);
	used = append_cstr(line, sizeof(line), used, ",\"timed_out\":false,\"outputs\":[");
	for (i = 0; i < probe_count; ++i) {
		char preview[8192];
		char digest[65];
		if (i != 0)
			used = append_char(line, sizeof(line), used, ',');
		probe_preview_and_digest(&probes[i], preview, sizeof(preview), digest, sizeof(digest));
		used = append_cstr(line, sizeof(line), used, "{\"label\":");
		used = append_json_string(line, sizeof(line), used, probes[i].label);
		used = append_cstr(line, sizeof(line), used, ",\"arg_index\":");
		used = append_i64(line, sizeof(line), used, probes[i].arg_index);
		used = append_cstr(line, sizeof(line), used, ",\"length\":");
		used = append_u64(line, sizeof(line), used, probes[i].length);
		used = append_cstr(line, sizeof(line), used, ",\"preview_hex\":");
		used = append_json_string(line, sizeof(line), used, preview);
		used = append_cstr(line, sizeof(line), used, ",\"sha256\":");
		used = append_json_string(line, sizeof(line), used, digest);
		if (probes[i].resource_kind_fd) {
			size_t j;
			used = append_cstr(line, sizeof(line), used, ",\"resource_kind\":\"fd\",\"resource_values\":[");
			for (j = 0; j < probes[i].resource_count; ++j) {
				if (j != 0)
					used = append_char(line, sizeof(line), used, ',');
				used = append_i64(line, sizeof(line), used, probes[i].resource_values[j]);
			}
			used = append_char(line, sizeof(line), used, ']');
		}
		used = append_char(line, sizeof(line), used, '}');
	}
	used = append_cstr(line, sizeof(line), used, "]}\n");
	emit_trace_line(line, used);
}

long traced_syscall(const char* syscall_name, long syscall_number, long call_index,
		    long a0, long a1, long a2, long a3, long a4, long a5)
{
	long args[6] = {a0, a1, a2, a3, a4, a5};
	uint64 start_ns = monotonic_ns();
	errno = 0;
	long ret = syscall(syscall_number, a0, a1, a2, a3, a4, a5);
	int saved_errno = errno;
	uint64 end_ns = monotonic_ns();
	struct output_probe probes[3];
	size_t probe_count = collect_outputs(syscall_number, ret, a0, a1, a2, a3, probes);
	long traced_ret = ret;
	int traced_errno = saved_errno;
	struct syzabi_injection injection = syzabi_get_injection();
	if (is_injection_match(injection, call_index, syscall_name) && injection.field &&
	    strcmp(injection.field, "return") == 0) {
		traced_ret = injection.value;
	}
	if (is_injection_match(injection, call_index, syscall_name) && injection.field &&
	    strcmp(injection.field, "errno") == 0) {
		traced_errno = (int)injection.value;
	}
	emit_event(syscall_name, syscall_number, call_index, args, traced_ret, traced_errno, start_ns,
		  end_ns, probes, probe_count);
	errno = saved_errno;
	return ret;
}

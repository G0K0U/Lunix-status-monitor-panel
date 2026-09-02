// SPDX-License-Identifier: GPL-2.0-or-later
/* Read the MPS HID feature report through read-only usbfs access. */
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/usbdevice_fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define REPORT_ID 0x02
#define REPORT_SIZE 0x76

static uint16_t le16(const uint8_t *p)
{
	return (uint16_t)p[0] | ((uint16_t)p[1] << 8);
}

int main(int argc, char **argv)
{
	const char *path = argc > 1 ? argv[1] : "/dev/bus/usb/001/013";
	uint8_t report[REPORT_SIZE] = { 0 };
	struct usbdevfs_ctrltransfer control = {
		.bRequestType = 0xa1, /* IN | class | interface */
		.bRequest = 0x01,     /* HID GET_REPORT */
		.wValue = (0x03 << 8) | REPORT_ID, /* feature report 2 */
		.wIndex = 0,
		.wLength = REPORT_SIZE,
		.timeout = 2000,
		.data = report,
	};
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	int ret;
	size_t i;

	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", path, strerror(errno));
		return EXIT_FAILURE;
	}
	ret = ioctl(fd, USBDEVFS_CONTROL, &control);
	if (ret < 0) {
		fprintf(stderr, "USBDEVFS_CONTROL: %s\n", strerror(errno));
		close(fd);
		return EXIT_FAILURE;
	}
	close(fd);

	printf("report_bytes=%d\n", ret);
	printf("report_id=%u\n", report[0]);
	printf("firmware_raw=%u\n", le16(report + 0x03));
	printf("serial_raw=%u\n", le16(report + 0x09));
	printf("pressure_raw=%u\n", le16(report + 0x11));
	printf("pressure_offset_raw=%" PRId16 "\n", (int16_t)le16(report + 0x19));
	printf("pressure_normalized_raw=%u\n", le16(report + 0x1b));
	printf("field_0x23_raw=%u\n", le16(report + 0x23));
	printf("temperature_external_raw=%u\n", le16(report + 0x2b));
	printf("temperature_internal_raw=%u\n", le16(report + 0x2d));
	printf("hex=");
	for (i = 0; i < (size_t)ret; i++)
		printf("%02x%s", report[i], i + 1 == (size_t)ret ? "\n" : " ");
	return EXIT_SUCCESS;
}

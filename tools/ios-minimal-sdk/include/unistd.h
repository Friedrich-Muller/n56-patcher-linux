#ifndef N56_MINI_UNISTD_H
#define N56_MINI_UNISTD_H

typedef long ssize_t;
ssize_t write(int fd, const void *buf, unsigned long nbyte);
ssize_t read(int fd, void *buf, unsigned long nbyte);
int open(const char *path, int flags, ...);
int close(int fd);
void sync(void);

#endif

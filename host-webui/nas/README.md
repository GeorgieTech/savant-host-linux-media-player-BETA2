# NAS runtime (Gigawatt)

FUSE helper for SMB mounts. Kernel CIFS is **not** in this Savant image.

- `fusermount` + `lib/libfuse.so.2` from Debian armhf fuse 2.9.9
- `rclone` linux-arm (static) is installed on the host as `/data/opt/nas/rclone` and is not stored in git (large)

Mount point: `/data/nas`. Settings starts and stops the mount.

rclone uses `--vfs-cache-mode writes` with `--vfs-cache-max-size 256M` under `/data/gigawatt/rclone-vfs`. Remount from Settings to pick up flag changes.

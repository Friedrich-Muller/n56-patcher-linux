"""iOS 12.5.8 SSH ramdisk 会话：引导 + iproxy + SSH + 挂卷 + tar 落盘。

移植自 boot_ramdisk.sh / install_ios13.sh / run_rootfs_stream.sh / apfs_setrole.py 的 SSH 语义。

关键约束（全部踩过坑，务必保留）：
  - 端口 2222→22（iproxy）。密码 alpine。
  - 挂载点：s1→/mnt1(只读)、s4→/mnt6、s5→/mnt7。
  - **绝不挂载 s2（iOS12 加密 Data）**：ramdisk 下连只读挂载都会卡死并锁住整个容器，
    之后对 rdisk0s1 的写入全部 Resource busy，只能重启。
  - ramdisk 根只读，无 /tmp、ps、df、shasum、dirname；chown 不在 PATH（用 /usr/sbin/chown）。
  - 远端 tar 后面绝不接 | head：SIGPIPE 会把 tar 中途杀掉，导致 rootfs 不完整。
  - 设备端解包一律 tar --numeric-owner，否则属主会变成 501:20。
"""
import hashlib
import os
import plistlib
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

from . import log
from .tarutil import normalize_args, owner_args
from .tools import run
from .usbmux import LegacyUsbMuxd

SEP_FIRMWARE = "/mnt1/usr/standalone/firmware/sep-firmware.img4"

RAMDISK_SEND = ["iBSS", "iBEC", "ramdisk", "devicetree", "trustcache", "kernelcache"]


class Ramdisk:
    def __init__(self, ctx):
        self.ctx = ctx
        self.port = ctx.t.get("ramdisk_port", 2222)
        self.password = ctx.t.get("ramdisk_password", "alpine")
        self._proxy = None
        self._usbmux = LegacyUsbMuxd()
        self._ssh_base = [
            ctx.tool("sshpass"), "-p", self.password, "ssh",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=10",
            "-p", str(self.port), "root@localhost",
        ]

    # ---- 设备状态 ----
    def dfu_wait(self):
        irecovery = self.ctx.tool("irecovery")
        log.log("等待设备进入 DFU（关机后按住 Home+电源）…")
        while True:
            r = run([irecovery, "-q"], check=False, timeout=10)
            if b"MODE: DFU" in (r.stdout or b""):
                return
            time.sleep(1)

    def pwn(self):
        irecovery = self.ctx.tool("irecovery")
        gaster = self.ctx.tool("gaster")
        q = run([irecovery, "-q"], check=False, timeout=10).stdout or b""
        if b"PWND" not in q:
            log.log("gaster pwn…")
            run([gaster, "pwn"], capture=False)
        log.log("gaster reset…")
        run([gaster, "reset"], capture=False, check=False)
        time.sleep(1)

    # ---- 引导 ramdisk ----
    def ensure_images(self):
        """Generate the ignored SSHRD images on first use."""
        rd = self.ctx.t["ramdisk_dir"]
        rd_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), rd)
        required = ["iBSS", "iBEC", "ramdisk", "devicetree", "trustcache", "kernelcache"]

        def missing_images():
            return [name for name in required
                    if not os.path.isfile(os.path.join(rd_dir, f"{name}.img4"))]

        missing = missing_images()
        if missing:
            script = os.path.join(os.path.dirname(rd_dir), "sshrd.sh")
            if not os.path.isfile(script):
                log.die(f"缺少 SSHRD 构建脚本: {script}")
            log.log("SSH ramdisk 镜像不存在或不完整，正在生成 iOS 12.5.8 镜像（需要网络和 DFU）…")
            script_to_run = script
            if sys.platform.startswith("linux"):
                repo = os.path.dirname(os.path.dirname(__file__))
                helper = os.path.join(repo, "tools", "sshrd_plist.py")
                replacement = f"{shlex.quote(sys.executable)} {shlex.quote(helper)}"
                with open(script, encoding="utf-8") as source:
                    contents = source.read()
                contents = contents.replace("../Linux/PlistBuddy", replacement)
                contents = contents.replace("Linux/PlistBuddy", replacement)
                descriptor, script_to_run = tempfile.mkstemp(
                    prefix=".sshrd-linux-", suffix=".sh", dir=os.path.dirname(script)
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as patched:
                    patched.write(contents)
            try:
                result = subprocess.run(
                    ["bash", script_to_run, "12.5.8"], cwd=os.path.dirname(script)
                )
            finally:
                if script_to_run != script:
                    os.unlink(script_to_run)
            if result.returncode != 0:
                log.die(f"SSHRD 镜像生成失败，退出码 {result.returncode}")
            missing = missing_images()
            if missing:
                files = ", ".join(f"{name}.img4" for name in missing)
                log.die(f"SSHRD 镜像生成后仍缺少: {files}")

    def boot(self):
        """发送 SSHRD 启动链（iBSS→iBEC→ramdisk→DT→TC→KC→bootx），然后起 iproxy。"""
        rd = self.ctx.t["ramdisk_dir"]
        self.ensure_images()
        rd_dir = self.ctx.local(rd)
        irecovery = self.ctx.tool("irecovery")

        def img(name):
            return os.path.join(rd_dir, f"{name}.img4")

        self.dfu_ensure()
        self.pwn()
        log.step(1, 6, "发送 iBSS")
        run([irecovery, "-f", img("iBSS")], capture=False); time.sleep(2)
        log.step(2, 6, "发送 iBEC")
        run([irecovery, "-f", img("iBEC")], capture=False); time.sleep(3)
        logo = img("logo")
        if os.path.exists(logo):
            run([irecovery, "-f", logo], capture=False, check=False)
            run([irecovery, "-c", "setpicture 0x1"], capture=False, check=False)
        log.step(3, 6, "发送 Ramdisk")
        run([irecovery, "-f", img("ramdisk")], capture=False)
        run([irecovery, "-c", "ramdisk"], capture=False); time.sleep(1)
        log.step(4, 6, "发送 DeviceTree")
        run([irecovery, "-f", img("devicetree")], capture=False)
        run([irecovery, "-c", "devicetree"], capture=False); time.sleep(1)
        log.step(5, 6, "发送 TrustCache")
        run([irecovery, "-f", img("trustcache")], capture=False)
        run([irecovery, "-c", "firmware"], capture=False); time.sleep(1)
        log.step(6, 6, "发送 KernelCache + bootx")
        run([irecovery, "-f", img("kernelcache")], capture=False)
        run([irecovery, "-c", "bootx"], capture=False)
        log.log("ramdisk 启动链已发送，等待 SSH 就绪…")
        self.wait_ssh(boot=True)

    def dfu_ensure(self):
        irecovery = self.ctx.tool("irecovery")
        r = run([irecovery, "-q"], check=False, timeout=10).stdout or b""
        if b"MODE: DFU" not in r:
            self.dfu_wait()

    # ---- iproxy / SSH ----
    def start_proxy(self):
        if self._proxy and self._proxy.poll() is None:
            return
        self._usbmux.start()
        configured_port = self.port
        for candidate in range(configured_port, min(configured_port + 101, 65536)):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
            self.port = candidate
            break
        else:
            log.die(f"本地端口 {configured_port} 至 65535 均不可用")
        if self.port != configured_port:
            port_arg = self._ssh_base.index("-p") + 1
            self._ssh_base[port_arg] = str(self.port)
            log.warn(f"本地端口 {configured_port} 已占用，SSH 代理改用 {self.port}")
        self._proxy = subprocess.Popen(
            [self.ctx.tool("iproxy"), str(self.port), "22"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        if self._proxy.poll() is not None:
            self._proxy = None
            log.die("iproxy 启动失败；请检查 USB 设备连接和 usbmuxd")

    def close(self):
        if self._proxy and self._proxy.poll() is None:
            self._proxy.terminate()
            try:
                self._proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proxy.kill()
                self._proxy.wait()
        self._proxy = None
        self._usbmux.close()

    def wait_ssh(self, boot=False, tries=120):
        self.start_proxy()
        for _ in range(tries):
            if self._alive():
                self.ctx._ssh = self
                log.log("ramdisk SSH 已连通。")
                return
            time.sleep(1)
        log.die("ramdisk SSH 不通" + ("（ramdisk 可能未起来）" if boot else "，先执行 ramdisk 阶段"))

    def _alive(self):
        return subprocess.run(self._ssh_base + ["true"], capture_output=True).returncode == 0

    def ensure(self):
        """保证 SSH 可用（iproxy 可能被 rootfs 流式拷贝杀掉）。"""
        self.start_proxy()
        if not self._alive():
            self.wait_ssh()
        self.ctx._ssh = self

    # ---- 远端命令 ----
    def sh(self, cmd, data=None, check=True, timeout=120):
        r = subprocess.run(self._ssh_base + [cmd], input=data,
                           capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            log.die(f"远端命令失败({r.returncode}): {cmd}\n"
                    f"{(r.stderr or b'').decode(errors='replace')}")
        return r.stdout or b""

    def ssh_argv(self):
        return list(self._ssh_base)

    # ---- 挂卷 ----
    def slices(self):
        return self.sh("ls /dev/disk0s1s*").decode(errors="replace").split()

    def mount(self, slice_no, mnt, ro=False):
        opt = "-o ro " if ro else ""
        self.sh(f'/sbin/mount | grep -q "disk0s1s{slice_no} on {mnt}" || '
                f'/sbin/mount_apfs {opt}/dev/disk0s1s{slice_no} {mnt}')

    def umount(self, *mnts):
        for m in mnts:
            self.sh(f"/sbin/umount {m}", check=False)

    def sync(self):
        self.sh("sync")

    # ---- SEP ----
    def _build_sep_firmware(self):
        im4m = self.ctx.im4m()
        with open(im4m, "rb") as ticket:
            ticket_id = hashlib.sha256(ticket.read()).hexdigest()[:16]
        output = self.ctx.wk(f"sep-firmware.n56-{ticket_id}.img4")
        im4p = self.ctx.wk("sep-firmware.n56.im4p")
        if os.path.isfile(output) and os.path.getsize(output) > 0:
            return output
        ipsw = self.ctx.ipsw_input("n56")
        with zipfile.ZipFile(ipsw) as archive:
            manifest = plistlib.loads(archive.read("BuildManifest.plist"))
            sep_member = None
            for identity in manifest.get("BuildIdentities", []):
                info = identity.get("Info", {})
                if info.get("DeviceClass") != self.ctx.t["device"]["model"]:
                    continue
                sep = identity.get("Manifest", {}).get("SEP", {})
                sep_member = sep.get("Info", {}).get("Path")
                if sep_member:
                    break
            if not sep_member:
                log.die("N56 iOS 12 IPSW 中找不到 SEP firmware component")
            os.makedirs(os.path.dirname(im4p), exist_ok=True)
            with archive.open(sep_member) as source, open(im4p, "wb") as target:
                shutil.copyfileobj(source, target)

        log.log(f"从 N56 IPSW 个性化 SEP firmware: {sep_member} (sepi)…")
        run([self.ctx.tool("img4"), "-i", im4p, "-o", output,
             "-M", im4m, "-T", "sepi"])
        if not os.path.isfile(output) or os.path.getsize(output) == 0:
            log.die("个性化 SEP IMG4 为空")
        return output

    def ensure_sep(self):
        """Ensure SEP is loaded; /mnt1 is read-only and /mnt6 must be writable for IPSW fallback."""
        ping = "/usr/libexec/seputil --ping 2>&1"
        ping_result = self.sh(ping, check=False)
        firmware = SEP_FIRMWARE
        present = self.sh(f'test -s "{firmware}" && echo yes || true', check=False)
        staged_firmware = "/mnt6/.n56-sep-firmware.img4"
        created_firmware = False
        if b"yes" not in present:
            log.warn("原 iOS 12 system volume 没有 sep-firmware.img4，改从 IPSW 个性化加载。")
            writable = self.sh('test -d /mnt6 && test -w /mnt6 && echo yes || true', check=False)
            if b"yes" not in writable:
                log.die("缺少原生 SEP IMG4，且 /mnt6 不可写；请先挂载已存在的 SystemB 后重试。")
            local_firmware = self._build_sep_firmware()
            self.deploy_path(local_firmware, staged_firmware)
            firmware = staged_firmware
            created_firmware = True
        if b"ping failed" not in ping_result.lower():
            log.log("SEP ping 已响应: " + ping_result.decode(errors="replace").strip())
            return
        log.log("加载 SEP firmware IMG4…")
        try:
            load_result = self.sh(
                f'/usr/libexec/seputil --load "{firmware}" 2>&1',
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            if created_firmware:
                self.sh(f'rm -f "{staged_firmware}"', check=False)
            output = exc.stdout or exc.stderr or b""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            log.die("seputil --load SEP 超时；未创建 DataB。请检查 ramdisk 的 SEP 驱动状态。\n"
                    + output.strip())
        log.log("SEP load: " + load_result.decode(errors="replace").strip())
        ping_result = self.sh(ping, check=False)
        if b"ping failed" in ping_result.lower():
            if created_firmware:
                self.sh(f'rm -f "{staged_firmware}"', check=False)
            log.die("SEP 加载后仍无响应，拒绝创建加密 APFS 卷以避免锁住容器。\n"
                    + ping_result.decode(errors="replace").strip())
        log.log("SEP ping 已响应: " + ping_result.decode(errors="replace").strip())

    # ---- tar 落盘 ----
    def deploy_tar_bytes(self, tar_bytes, dest_mnt):
        """把一段 tar 字节流解包到设备 dest_mnt（保属主）。"""
        self.sh(f"COPYFILE_DISABLE=1 /usr/bin/tar --numeric-owner -xpf - -C {dest_mnt}",
                data=tar_bytes)

    @staticmethod
    def _compressed_paths(root):
        """返回 root 下带 Apple 压缩 xattr/flag 的相对路径（bytes）。

        设备端 tar 读这些文件会走 copyfile pack，遇到 com.apple.decmpfs 时
        返回 EPERM。主机侧 apfs-fuse/hdiutil 会透明解压，所以这部分改从本地
        rootfs 流过去。
        """
        result = set()
        compressed_flag = getattr(stat, "UF_COMPRESSED", 0x20)
        for base, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                path = os.path.join(base, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                xattrs = ()
                try:
                    xattrs = os.listxattr(path, follow_symlinks=False)
                except TypeError:
                    xattrs = os.listxattr(path)
                except OSError:
                    pass
                compressed = "com.apple.decmpfs" in xattrs
                if not compressed:
                    try:
                        compressed = bool(os.stat(path, follow_symlinks=False).st_flags
                                          & compressed_flag)
                    except (AttributeError, OSError):
                        pass
                if compressed:
                    rel = os.path.relpath(path, root).replace(os.sep, "/")
                    result.add(os.fsencode(rel))
        return result

    def list_remote_paths(self, root):
        """列出设备目录树中的相对路径（bytes），优先使用 NUL 分隔。"""
        quoted = shlex.quote(root)
        for finder in ("/usr/bin/find", "find"):
            out = self.sh(f"cd {quoted} && {finder} . -print0 2>/dev/null", check=False)
            if b"\0" in out:
                return [item for item in out.split(b"\0") if item]
        out = self.sh(f"cd {quoted} && /usr/bin/find . -print 2>/dev/null", check=False)
        if not out:
            out = self.sh(f"cd {quoted} && find . -print 2>/dev/null", check=False)
        return [line for line in out.splitlines() if line]

    @staticmethod
    def _relative_tar_name(name):
        if name == b".":
            return b""
        if name.startswith(b"./"):
            name = name[2:]
        return name

    def _remote_tar_pipeline(self, producer, consumer, timeout, temp_dir):
        """在设备上并行跑 tar 生产/消费，分别收集 stderr 与退出码。

        ramdisk 没有 /tmp，临时文件必须放在已挂载的可写卷上。
        """
        paths = {key: shlex.quote(os.path.join(temp_dir, f".n56-remote-tar.{key}"))
                 for key in ("src.err", "dst.err", "src.status", "dst.status")}
        err_src, err_dst = paths["src.err"], paths["dst.err"]
        st_src, st_dst = paths["src.status"], paths["dst.status"]
        cmd = (
            f"/bin/rm -f {err_src} {err_dst} {st_src} {st_dst}; "
            f"{{ {producer} 2>{err_src}; echo $? >{st_src}; }} | "
            f"{{ {consumer} 2>{err_dst}; echo $? >{st_dst}; }}; "
            f"cat {err_src} {err_dst} >&2; "
            f"src_status=$(cat {st_src}); dst_status=$(cat {st_dst}); "
            f"/bin/rm -f {err_src} {err_dst} {st_src} {st_dst}; "
            f'test "$src_status" = 0 && test "$dst_status" = 0'
        )
        self.sh(cmd, timeout=timeout)

    def merge_remote_dir(self, source, dest, local_source=None, timeout=3600):
        """把设备上一个目录树流式合并到另一个目录树。

        不能用 ``cp -a`` 合并 DataB：GNU cp 会重放 xattr/flags，并对已存在的
        符号链接报 File exists/EPERM。设备端 tar 读 Apple 压缩文件（如
        com.apple.decmpfs）也会 EPERM，所以这类文件从主机已挂载的 rootfs 流过去；
        其余路径仍在设备端 tar | tar，保留属主、权限、时间和符号链接。
        """
        source_path = source
        dest_path = dest
        source = shlex.quote(source)
        dest = shlex.quote(dest)
        mount = source_path.strip("/").split("/", 1)[0]
        temp_dir = f"/{mount}" if mount else "/"
        compressed = self._compressed_paths(local_source) if local_source else set()
        remote_names = [self._relative_tar_name(name)
                        for name in self.list_remote_paths(source_path)]
        remote_names = [name for name in remote_names if name and name not in compressed]

        if remote_names:
            list_file = shlex.quote(os.path.join(temp_dir, ".n56-merge-paths"))
            self.sh(f"cat > {list_file}", data=b"".join(name + b"\0" for name in remote_names))
            # 设备上的 tar 对从 -T 读到的名字不应用 -C，必须在源目录内启动，
            # 否则每个相对路径都会被当成当前工作目录下的路径而 Cannot stat。
            producer = (f"( cd {source} && /usr/bin/tar --numeric-owner --no-recursion "
                        f"--null -T {list_file} -cpf - )")
            consumer = f"/usr/bin/tar --numeric-owner --overwrite -xpf - -C {dest}"
            try:
                self._remote_tar_pipeline(producer, consumer, timeout, temp_dir)
            finally:
                self.sh(f"/bin/rm -f {list_file}", check=False)

        if compressed:
            self.deploy_relative_paths(local_source,
                                       [os.fsdecode(name) for name in sorted(compressed)],
                                       dest_path, overwrite=True, timeout=timeout)

    def read_tar(self, remote_path_expr):
        """从设备打包并读回（例如备份）。remote_path_expr 形如 '-C /mnt7 keybags'。"""
        return self.sh(f"/usr/bin/tar -cf - {remote_path_expr}")

    def deploy_path(self, source, dest, uid=None, gid=None):
        """流式写入本地文件或目录，不生成中间 tar。"""
        source = os.path.abspath(source)
        if os.path.isdir(source):
            self.sh(f'mkdir -p "{dest}"')
            command = ["/usr/bin/tar", "--no-xattrs", *normalize_args(),
                       *owner_args(uid, gid), "-cpf", "-", "-C", source, "."]
            producer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=dict(os.environ, COPYFILE_DISABLE="1"))
            consumer = subprocess.Popen(
                self._ssh_base + [f'COPYFILE_DISABLE=1 /usr/bin/tar '
                                  f'--numeric-owner --overwrite -xpf - -C "{dest}"'],
                stdin=producer.stdout, stderr=subprocess.PIPE)
            producer.stdout.close()
            _, consumer_err = consumer.communicate()
            _, producer_err = producer.communicate()
            if producer.returncode or consumer.returncode:
                log.die(f"目录流式写入失败: {source} → {dest}\n"
                        f"{(producer_err or consumer_err or b'').decode(errors='replace')}")
            return
        self._deploy_file(source, dest)

    def deploy_relative_paths(self, root, paths, dest, overwrite=False, timeout=3600):
        """从本地 root 流式部署指定相对路径集合，保留目录结构。

        路径表经 NUL 分隔的临时文件传给 tar，避免数千个 Apple 压缩文件撑爆
        命令行；文件内容仍走 tar | ssh tar 流，不经主机落盘。
        """
        root = os.path.abspath(root)
        paths = list(paths)
        if not paths:
            return
        self.sh(f'mkdir -p {shlex.quote(dest)}')
        descriptor, list_path = tempfile.mkstemp(prefix=".n56-deploy-paths-", suffix=".lst")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                for path in paths:
                    stream.write(os.fsencode(path) + b"\0")
            command = ["/usr/bin/tar", "--no-xattrs", *normalize_args(), "--null",
                       "-cpf", "-", "-T", list_path]
            producer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        cwd=root,
                                        env=dict(os.environ, COPYFILE_DISABLE="1"))
            overwrite_arg = " --overwrite" if overwrite else ""
            consumer = subprocess.Popen(
                self._ssh_base + [f"COPYFILE_DISABLE=1 /usr/bin/tar "
                                  f"--numeric-owner{overwrite_arg} -xpf - "
                                  f"-C {shlex.quote(dest)}"],
                stdin=producer.stdout, stderr=subprocess.PIPE)
            producer.stdout.close()
            _, consumer_err = consumer.communicate(timeout=timeout)
            _, producer_err = producer.communicate(timeout=timeout)
            if producer.returncode or consumer.returncode:
                log.die("文件集流式写入失败\n" +
                        (producer_err or consumer_err or b"").decode(errors="replace"))
        finally:
            os.unlink(list_path)

    def pull_file(self, source, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as stream:
            proc = subprocess.run(self._ssh_base + [f'cat "{source}"'], stdout=stream,
                                  stderr=subprocess.PIPE)
        if proc.returncode:
            log.die(f"读取设备文件失败: {source}\n"
                    f"{(proc.stderr or b'').decode(errors='replace')}")
        return dest

    def _deploy_file(self, source, dest):
        command = f'mkdir -p "{os.path.dirname(dest)}"; cat > "{dest}"'
        with open(source, "rb") as stream:
            proc = subprocess.Popen(self._ssh_base + [command], stdin=stream, stderr=subprocess.PIPE)
            _, err = proc.communicate()
        if proc.returncode:
            log.die(f"文件流式写入失败: {source} → {dest}\n"
                    f"{(err or b'').decode(errors='replace')}")

    def read_range(self, path, offset, size):
        return self.sh(f'dd if="{path}" bs=1 skip={offset} count={size} 2>/dev/null')

    def write_range(self, path, offset, data):
        self.sh(f'dd of="{path}" bs=1 seek={offset} conv=notrunc 2>/dev/null',
                data=data, timeout=120)

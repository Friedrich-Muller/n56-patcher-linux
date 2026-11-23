"""为加密 DataB 重建 systembag，保留设备全局 BAG1。"""
from pathlib import Path
import subprocess

from core import log
from core.context import REPO
from core.iosbuild import build_macho
from core.tools import run

FRAMEWORK = "/System/Library/PrivateFrameworks/MobileKeyBag.framework/MobileKeyBag"
REMOTE = "/mnt6/.n56-bootstrap"
# KBSaveSystemBagHandle 第 5 参数：mov w4, #0 → #1，沿用已有 BAG1 包裹材料。
PRESERVE_BAG1 = (0x8d20, bytes.fromhex("04008052"), bytes.fromhex("24008052"))

# KBMapVolume 默认复用 APFS 卷里已存的 cookie。跨 ramdisk 会话这个 cookie
# 会失效，导致 aks_set_keybag_for_volume_with_cookie -> 0xe007c018。两处 NOP
# 让 APFSVolumePayloadGet 的成功分支也走 "Not cookie... KBSetCookie" 路径，
# 每次都用当前 SEP 会话重新生成并写回 volume cookie。
FORCE_NEW_VOLUME_COOKIE = (
    (0x5ee4, bytes.fromhex("e0060034")),  # cbz w0, 0x5fc0
    (0x5f0c, bytes.fromhex("21020054")),  # b.ne 0x5f50
)
NOP = bytes.fromhex("1f2003d5")


def patch_framework(data):
    patches = [PRESERVE_BAG1, *((off, before, NOP) for off, before in FORCE_NEW_VOLUME_COOKIE)]
    for offset, before, after in patches:
        if data[offset:offset + 4] != before:
            raise ValueError("MobileKeyBag 与 16H88 ramdisk 版本不符，拒绝打补丁")
        data = data[:offset] + after + data[offset + 4:]
    return data


def build_helper(ctx):
    source = Path(REPO) / "tools/systembag/create_systembag.c"
    output = ctx.wk("systembag/create_systembag")
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    build_macho(source, output, min_version="12.0", optimize="-O2")
    run(["codesign", "-s", "-", "-f", "--entitlements",
         str(source.with_suffix(".entitlements")), output])
    return output


def build_framework(ctx):
    native = ctx.ssh.pull_file(FRAMEWORK, ctx.wk("systembag/MobileKeyBag.native"))
    output = Path(ctx.wk("systembag/MobileKeyBag.preserve-BAG1"))
    output.write_bytes(patch_framework(Path(native).read_bytes()))
    run(["codesign", "-s", "-", "-f", str(output)])
    return str(output)


def _remote(ssh, cmd, check=True):
    """运行远端命令，失败时把 stdout 和 stderr 都带进报错。"""
    result = subprocess.run(ssh.ssh_argv() + [cmd], capture_output=True)
    out = (result.stdout or b"").decode(errors="replace")
    err = (result.stderr or b"").decode(errors="replace")
    if check and result.returncode:
        log.die(f"远端命令失败({result.returncode}): {cmd}\n{out}{err}")
    return result.returncode, out, err


def _run_helper(ssh, helper, cmd):
    """助手偶尔会在 main() 前被 AMFI 杀掉；重新部署后再试一次。"""
    for attempt in range(2):
        rc, out, err = _remote(ssh, cmd, check=False)
        print(out, end="")
        if err:
            print(err, end="")
        if rc == 137 and attempt == 0:
            log.log("helper 启动被杀死，重新部署后重试一次…")
            ssh.deploy_path(helper, f"{REMOTE}/create_systembag")
            ssh.sh(f'chmod 755 "{REMOTE}/create_systembag"')
            continue
        if rc:
            log.die(f"远端命令失败({rc}): {cmd}\n{out}{err}")
        return
    log.die("helper 连续两次启动失败")


def _wipe_data_b(ssh):
    """普通挂载下清空整个 DataB，包括旧的 keybags/systembag.kb。

    aks_load_bag → aks_set_system 这条重载路径在本 ramdisk 上无法把 systembag
    发布给 SEP（aks_get_system 仍为 0，卷映射返回 0xe007c018）。因此只走已知
    可用的 MKBKeyBagCreateSystem 创建路径；它要求卷上没有旧的 systembag 状态。
    """
    entries = ('for p in /mnt7/.[!.]* /mnt7/..?* /mnt7/*; do '
               '{ [ -e "$p" ] || [ -L "$p" ]; } || continue; ')
    ssh.sh('if command -v chgflags >/dev/null 2>&1; then '
           + entries + 'chgflags -R nouchg,noschg "$p" 2>/dev/null || true; done; fi')
    ssh.sh(entries + '/bin/rm -rf "$p"; done')
    leftover = ssh.sh(entries + 'echo "$p"; done', check=False).decode(errors="replace").strip()
    if leftover:
        log.die("DataB 清理后仍有残留，拒绝重建 systembag（请先手动处理）:\n" + leftover)


def ensure_systembag(ctx):
    """需已只读挂载 /mnt1、读写挂载 /mnt6；本函数加载 SEP 并挂载 /mnt7。"""
    ssh = ctx.ssh
    ssh.ensure_sep()
    device = f'/dev/disk0s1s{ctx.t["mounts"]["data_b"]}'

    # 回到首次成功创建时的状态：普通读写挂载清空 DataB，再 protect 挂载创建。
    ssh.sh("/sbin/umount /mnt7", check=False)
    ssh.sh(f"/sbin/mount_apfs {device} /mnt7")
    _wipe_data_b(ssh)
    ssh.sh("/sbin/umount /mnt7")
    ssh.sh(f"/sbin/mount_apfs -o protect {device} /mnt7")
    mount_line = ssh.sh('/sbin/mount | grep "on /mnt7 " || true').decode(errors="replace").strip()
    log.log(f"systembag 创建挂载状态: {mount_line}")
    ssh.sh("test ! -e /mnt7/keybags && test ! -L /mnt7/keybags")

    log.log("重建 systembag（保留 BAG1）…")
    helper = build_helper(ctx)
    framework = build_framework(ctx)
    ssh.deploy_path(helper, f"{REMOTE}/create_systembag")
    ssh.deploy_path(framework, f"{REMOTE}/MobileKeyBag.preserve-BAG1")
    ssh.sh(f'chmod 755 "{REMOTE}/create_systembag" "{REMOTE}/MobileKeyBag.preserve-BAG1"; '
           'mkdir -p /mnt7/keybags && chmod 700 /mnt7/keybags')
    cmd = (f'echo N56_CREATE_BEGIN; '
           f'"{REMOTE}/create_systembag" --create /mnt7 '
           f'"{REMOTE}/MobileKeyBag.preserve-BAG1"; '
           f'_n56_rc=$?; echo N56_CREATE_RC=$_n56_rc; exit $_n56_rc')
    _run_helper(ssh, helper, cmd)
    ssh.sh("echo n56-write-test > /mnt7/.n56-write-test && "
           "/bin/rm -f /mnt7/.n56-write-test")
    log.log("systembag 创建后 DataB 写入预检通过。")
    ssh.sh(f'rm -rf "{REMOTE}"; test -s /mnt7/keybags/systembag.kb')
    ssh.sync()

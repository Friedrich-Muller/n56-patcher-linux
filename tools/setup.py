#!/usr/bin/env python3
"""Fetch pinned upstream tools and build img4 for macOS."""
from pathlib import Path
import configparser
import platform
import shutil
import subprocess
import sys

from fetch_checkra1n import fetch as fetch_checkra1n

REPO = Path(__file__).resolve().parents[1]


def run(*args, cwd=REPO):
    subprocess.run(args, cwd=cwd, check=True)


def exclude_built(submodule, rules):
    exclude = subprocess.check_output(
        ["git", "rev-parse", "--git-path", "info/exclude"], cwd=submodule, text=True
    ).strip()
    exclude = submodule / exclude
    current = exclude.read_text() if exclude.exists() else ""
    if rules not in current:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(current + "\n" + rules)


def patch_capstone_for_py314():
    """capstone 4.0.2 predates the removal of distutils/pkg_resources."""
    init = REPO / "tools/bin/pydeps/capstone/__init__.py"
    if not init.exists():
        return
    text = init.read_text()
    patched = (text
               .replace("import distutils.sysconfig\nimport pkg_resources\n", "import sysconfig\n")
               .replace("pkg_resources.resource_filename(__name__, 'lib')",
                        "join(split(__file__)[0], 'lib')")
               .replace("distutils.sysconfig.get_python_lib()",
                        "sysconfig.get_paths()['purelib']"))
    if patched != text:
        init.write_text(patched)


def setup():
    modules = configparser.ConfigParser()
    modules.read(REPO / ".gitmodules")
    for name in ("SSHRD_Script", "img4lib", "devicetree-parse", "DyldExtractor", "gaster"):
        run("git", "submodule", "update", "--init", f"tools/{name}")

    # Keep the Darwin tools checkout small; firmware stays in donors/.
    path = "tools/dualra1n"
    dualra1n = REPO / path
    run("git", "submodule", "init", path)
    if not (dualra1n / ".git").exists():
        url = modules['submodule "tools/dualra1n"']["url"]
        run("git", "clone", "--filter=blob:none", "--no-checkout", url, str(dualra1n))
    run("git", "sparse-checkout", "set", "binaries/Darwin", "binaries/.licenses", "other/apfsios14",
        cwd=dualra1n)
    entry = subprocess.check_output(
        ["git", "ls-files", "--stage", path], cwd=REPO, text=True
    )
    commit = entry.split()[1]
    run("git", "fetch", "--depth", "1", "origin", commit, cwd=dualra1n)
    run("git", "checkout", "--detach", commit, cwd=dualra1n)
    run("git", "submodule", "absorbgitdirs", path)

    img4lib = REPO / "tools/img4lib"
    run("git", "submodule", "update", "--init", "lzfse", cwd=img4lib)
    run("make", "-C", str(img4lib / "lzfse"))
    run("make", "-C", str(img4lib))
    dtparse = REPO / "tools/devicetree-parse"
    run("make", "-C", str(dtparse), f"ARCH={platform.machine()}")
    exclude_built(dtparse, "*.o\n/devicetree-parse\n/devicetree-repack\n")

    # sshrd.sh downloads gaster only when this path is missing; that download is unreliable.
    gaster = REPO / "tools/gaster"
    target = REPO / "tools/SSHRD_Script/Darwin/gaster"
    if platform.system() == "Linux":
        from build_apfs_fuse import build as build_apfs_fuse
        from build_ldid import build as build_ldid
        build_apfs_fuse()
        build_ldid()

        linux_gaster = gaster / "gaster"
        linux_target = REPO / "tools/SSHRD_Script/Linux/gaster"

        if not linux_target.exists():
            raise FileNotFoundError(
                "Linux Gaster missing. Build tools/gaster first."
            )

        linux_target.chmod(0o755)

    elif not target.exists():
        run("make", "-C", str(gaster), "macos")
        exclude_built(gaster, "/gaster\n")
        shutil.copy2(gaster / "gaster", target)

    apfs14 = REPO / "donors/projects/dualra1n-bin/apfsios14"
    if not apfs14.exists():
        shutil.copytree(dualra1n / "other/apfsios14", apfs14, symlinks=True)
    fetch_checkra1n()

    run(sys.executable, "-m", "pip", "install", "-q", "--target", "tools/bin/pydeps",
        "progressbar2", "typing_extensions")
    run(sys.executable, "-m", "pip", "install", "-q", "--upgrade", "--no-deps",
        "--target", "tools/bin/pydeps", "capstone==4.0.2")
    patch_capstone_for_py314()
    print("工具已就绪：tools/SSHRD_Script/、tools/dualra1n/binaries/、tools/img4lib/img4、donors/")


if __name__ == "__main__":
    setup()

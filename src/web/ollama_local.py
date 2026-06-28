"""
========================================
web/ollama_local.py — 本地向量化「一键搭建」(非 Docker 用户)
========================================

背景：旧的「一键搭建本地向量化」只对 Docker 用户有效（ollama 跑在 ombre-ollama
容器里）。裸机/原生用户（pip 装、python src/server.py 跑在 Win/Linux/mac）机器上
根本没有 ollama，按钮只会提示去起容器。

本模块补上这块：检测宿主 → 免提权自动装 ollama 运行时 → 作为 OB 子进程常驻 →
（模型下载仍复用 embedding.py 的 /api/embedding/local/pull，已支持镜像）。

设计取舍（已与使用者确认）：
- 安装力度：免提权自动装 + 失败回退。Win 走 per-user 静默安装器（不需管理员）；
  Linux/mac 下静态二进制/包到用户目录（不需 sudo）。任何一步需要提权 / 被杀软拦 /
  下载失败 → 回退成「一键复制官方命令 + 指引」，绝不静默失败。
- 常驻方式：ollama serve 作为 OB 管理的子进程。OB 起它就起，OB 关它就关，
  挂了自动拉起。不写 systemd/launchd/注册表（跨平台最省、最可控）。

对外暴露：
- register(mcp)：注册 /api/embedding/local/env、/install(+status)、/start
- ensure_child_on_boot() / stop_child()：给 server.py lifespan 调
========================================
"""

import os
import sys
import shutil
import platform
import asyncio
import subprocess
import zipfile
import threading
import urllib.request

import httpx
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from . import _shared as sh

logger = sh.logger

_OLLAMA_PORT = 11434
_LOCAL_BASE = f"http://127.0.0.1:{_OLLAMA_PORT}"

# ollama 运行时（二进制/安装器）下载镜像。模型 registry 的镜像在 embedding.py 里另算。
_BIN_MIRRORS = {
    "official": "https://ollama.com/download",
    "github": "https://github.com/ollama/ollama/releases/latest/download",
}

# 各系统官方安装命令（自动装失败时回退给用户手动跑）
_MANUAL_HINT = {
    "windows": "去 https://ollama.com/download/windows 下载 OllamaSetup.exe 双击安装；装完回来再点一次。",
    "linux": "在终端运行：curl -fsSL https://ollama.com/install.sh | sh （需要 sudo）；装完回来再点一次。",
    "macos": "用 Homebrew：brew install ollama；或去 https://ollama.com/download/mac 下载 App。装完回来再点一次。",
}

# 安装进度（线程里跑阻塞下载，主循环只读这个 dict）
_install_state: dict = {
    "running": False, "phase": "idle", "percent": 0.0,
    "msg": "", "error": "", "hint": "",
}
_install_thread: "threading.Thread | None" = None

# 子进程管理
_child_proc: "subprocess.Popen | None" = None
_child_managed = False
_child_monitor_task: "asyncio.Task | None" = None


# ============================================================
# 环境探测
# ============================================================

def _os_key() -> str:
    s = sys.platform
    if s.startswith("win"):
        return "windows"
    if s == "darwin":
        return "macos"
    return "linux"


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("amd64", "x86_64", "x64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    return m or "amd64"


def _user_install_root() -> str:
    """免提权安装目标根目录（用户家目录下，不需 sudo/管理员）。"""
    return os.path.join(os.path.expanduser("~"), ".ollama", "local")


def find_ollama_bin() -> "str | None":
    """找 ollama 可执行文件：PATH 优先，再查各系统免提权安装位置。"""
    p = shutil.which("ollama")
    if p:
        return p
    osk = _os_key()
    home = os.path.expanduser("~")
    cands = []
    if osk == "windows":
        cands += [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
            os.path.join(_user_install_root(), "ollama.exe"),
        ]
    elif osk == "macos":
        cands += [
            os.path.join(_user_install_root(), "Ollama.app", "Contents", "Resources", "ollama"),
            "/Applications/Ollama.app/Contents/Resources/ollama",
        ]
    cands += [
        os.path.join(_user_install_root(), "bin", "ollama"),
        os.path.join(home, ".ollama", "bin", "ollama"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


async def _is_running(base: str = _LOCAL_BASE) -> bool:
    # trust_env=False：本地 ollama 必须绕过系统代理（Clash/V2Ray 等会把 127.0.0.1
    # 也丢给代理 → 502，明明 serve 在跑却判定挂了）。
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as c:
            r = await c.get(f"{base}/api/version")
            return r.status_code == 200
    except Exception:
        return False


async def _detect() -> dict:
    osk = _os_key()
    bin_path = find_ollama_bin()
    running = await _is_running()
    in_docker = sh.in_docker()
    return {
        "ok": True,
        "os": osk,
        "arch": _arch(),
        "in_docker": in_docker,
        "installed": bool(bin_path),
        "bin_path": bin_path or "",
        "running": running,
        "managed": _child_managed,
        "mirrors": list(_BIN_MIRRORS.keys()),
        # 给前端决定下一步：docker → 走容器；裸机 → 装/起/拉
        "recommended": _recommend(in_docker, bool(bin_path), running),
        "manual_hint": _MANUAL_HINT.get(osk, ""),
    }


def _recommend(in_docker: bool, installed: bool, running: bool) -> str:
    if in_docker:
        return "docker"          # 容器部署：用 compose 的 ollama 服务，前端给容器指引
    if running:
        return "pull"            # 已在跑：直接拉模型
    if installed:
        return "start"           # 装了没跑：起子进程
    return "install"             # 啥都没有：先装


# ============================================================
# 安装（线程里跑，免提权）
# ============================================================

_GH_RELEASES = "https://github.com/ollama/ollama/releases/latest/download"
_OLLAMA_DL = "https://ollama.com/download"


def _bin_url(mirror: str, osk: str, arch: str) -> str:
    """按系统给出运行时下载地址。

    实测（v0.30.x）：
    - Win/mac 的安装包 OllamaSetup.exe / Ollama-darwin.zip 在 ollama.com/download
      与 GitHub Releases 都有。
    - Linux 只有 GitHub Releases 提供 `ollama-linux-{arch}.tar.zst`（zstd 压缩，
      不是 tgz；ollama.com/download 不托管它，会 404）。
    custom 镜像（既不是 official 也不是 github）直接拿来当前缀拼资源名。
    """
    if osk == "windows":
        name = "OllamaSetup.exe"
    elif osk == "macos":
        name = "Ollama-darwin.zip"
    else:
        name = f"ollama-linux-{arch}.tar.zst"
    if mirror == "official" and osk in ("windows", "macos"):
        base = _OLLAMA_DL
    elif mirror in ("official", "github"):
        base = _GH_RELEASES          # linux 强制走 GitHub Releases；github 同理
    else:
        base = mirror.rstrip("/")     # 自定义镜像前缀（如 github 代理）
    return f"{base}/{name}"


def _extract_zst(path: str, dest: str) -> bool:
    """解 .tar.zst 到 dest。优先系统 tar（--zstd / unzstd），回退 python zstandard。成功 True。"""
    for cmd in (
        ["tar", "--zstd", "-xf", path, "-C", dest],
        ["tar", "--use-compress-program=unzstd", "-xf", path, "-C", dest],
    ):
        try:
            if subprocess.run(cmd, capture_output=True, timeout=600).returncode == 0:
                return True
        except Exception:
            continue
    try:
        import io as _io
        import tarfile as _tf
        import zstandard  # type: ignore
        with open(path, "rb") as f:
            with zstandard.ZstdDecompressor().stream_reader(f) as reader:
                with _tf.open(fileobj=_io.BufferedReader(reader), mode="r|") as t:
                    t.extractall(dest)
        return True
    except Exception:
        return False


def _download(url: str, dest: str) -> None:
    """流式下载，进度写 _install_state['percent']。"""
    req = urllib.request.Request(url, headers={"User-Agent": "OmbreBrain-Setup"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    _install_state["percent"] = round(done / total * 100, 1)
    _install_state["percent"] = 100.0


def _install_run(osk: str, arch: str, mirror: str) -> None:
    """阻塞安装流程（在线程里）。任何失败都落到 _install_state.error + hint，不抛。"""
    global _install_state
    tmpdir = os.path.join(_user_install_root(), "_dl")
    try:
        os.makedirs(tmpdir, exist_ok=True)
        url = _bin_url(mirror, osk, arch)
        _install_state.update(phase="downloading", percent=0.0, msg=f"下载 ollama 运行时…（{url}）")
        logger.info(f"[ollama-install] downloading {url}")

        if osk == "windows":
            exe = os.path.join(tmpdir, "OllamaSetup.exe")
            _download(url, exe)
            _install_state.update(phase="installing", msg="静默安装中（per-user，无需管理员）…")
            # Ollama 的 Windows 安装器（Inno Setup）默认就装到 %LOCALAPPDATA%\Programs\Ollama，
            # per-user、不需管理员。静默装即可；不传 /CURRENTUSER 以免个别版本不允许覆盖而报错。
            r = subprocess.run(
                [exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                capture_output=True, timeout=600,
            )
            if r.returncode not in (0, 3010):  # 3010 = 需重启，但二进制已就位
                raise RuntimeError(f"安装器返回 {r.returncode}: {(r.stderr or b'')[:200].decode('utf-8','replace')}")

        elif osk == "linux":
            tzst = os.path.join(tmpdir, "ollama.tar.zst")
            _download(url, tzst)
            _install_state.update(phase="extracting", msg="解压到用户目录（无需 sudo）…")
            root = _user_install_root()
            os.makedirs(root, exist_ok=True)
            if not _extract_zst(tzst, root):  # 解出 bin/ollama + lib/
                raise RuntimeError(
                    "解压 .tar.zst 失败（系统缺 zstd/tar 支持）。"
                    "可改用官方脚本：curl -fsSL https://ollama.com/install.sh | sh"
                )
            binp = os.path.join(root, "bin", "ollama")
            if os.path.isfile(binp):
                os.chmod(binp, 0o755)

        elif osk == "macos":
            zp = os.path.join(tmpdir, "Ollama-darwin.zip")
            _download(url, zp)
            _install_state.update(phase="extracting", msg="解压 App 到用户目录…")
            root = _user_install_root()
            os.makedirs(root, exist_ok=True)
            with zipfile.ZipFile(zp) as z:
                z.extractall(root)
            binp = os.path.join(root, "Ollama.app", "Contents", "Resources", "ollama")
            if os.path.isfile(binp):
                os.chmod(binp, 0o755)
        else:
            raise RuntimeError(f"不支持的系统：{osk}")

        # 校验真的装上了
        if not find_ollama_bin():
            raise RuntimeError("安装流程跑完但没找到 ollama 可执行文件")
        _install_state.update(phase="done", percent=100.0, msg="安装完成 ✓", error="", hint="")
        logger.info("[ollama-install] done")
    except Exception as e:
        _install_state.update(
            phase="error",
            error=f"{type(e).__name__}: {e}"[:300],
            hint="可换下载镜像重试（official ↔ github），或按提示手动安装：" + _MANUAL_HINT.get(osk, ""),
        )
        logger.warning(f"[ollama-install] failed: {e}")
    finally:
        _install_state["running"] = False
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ============================================================
# 子进程常驻
# ============================================================

def _spawn() -> "subprocess.Popen | None":
    binp = find_ollama_bin()
    if not binp:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _os_key() == "windows" else 0
    env = os.environ.copy()
    env.setdefault("OLLAMA_HOST", f"127.0.0.1:{_OLLAMA_PORT}")
    return subprocess.Popen(
        [binp, "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, creationflags=flags,
    )


async def ensure_child() -> dict:
    """确保 ollama 在跑：已可达→直接用；装了没跑→拉起子进程并等就绪；没装→报缺失。"""
    global _child_proc, _child_managed
    if await _is_running():
        return {"running": True, "managed": _child_managed, "reason": "already_running"}
    if sh.in_docker():
        return {"running": False, "managed": False, "reason": "in_docker"}
    if not find_ollama_bin():
        return {"running": False, "managed": False, "reason": "not_installed"}
    try:
        _child_proc = _spawn()
    except Exception as e:
        return {"running": False, "managed": False, "reason": f"spawn_failed: {e}"}
    if _child_proc is None:
        return {"running": False, "managed": False, "reason": "not_installed"}
    # 等就绪：首次冷启动很慢——实测 Windows 全新安装后第一次 `ollama serve`
    # 要做运行时/GPU 探测，可能 >150s 才开始监听 11434。给到 ~180s，
    # 每秒探一次（_is_running 自带 3s 超时，端口已开但慢响应时不会误判失败）。
    for _ in range(180):
        if await _is_running():
            _child_managed = True
            _start_monitor()
            logger.info("[ollama] child serve started & ready")
            return {"running": True, "managed": True, "reason": "spawned"}
        await asyncio.sleep(1)
    return {"running": False, "managed": False, "reason": "spawn_timeout"}


def _start_monitor() -> None:
    global _child_monitor_task
    if _child_monitor_task is None or _child_monitor_task.done():
        _child_monitor_task = asyncio.create_task(_monitor())


async def _monitor() -> None:
    """子进程挂了自动拉起（仅限我们托管的那只）。"""
    global _child_proc
    while _child_managed:
        await asyncio.sleep(5)
        try:
            if _child_proc is not None and _child_proc.poll() is not None:
                logger.warning("[ollama] managed child exited, respawning")
                _child_proc = _spawn()
        except Exception as e:
            logger.warning(f"[ollama] monitor respawn failed: {e}")


async def stop_child() -> None:
    """OB 关停时一并停掉我们托管的 ollama 子进程。"""
    global _child_proc, _child_managed
    _child_managed = False
    if _child_monitor_task:
        _child_monitor_task.cancel()
    if _child_proc is not None and _child_proc.poll() is None:
        try:
            _child_proc.terminate()
            try:
                _child_proc.wait(timeout=5)
            except Exception:
                _child_proc.kill()
        except Exception:
            pass
    _child_proc = None


async def ensure_child_on_boot() -> None:
    """server.py lifespan 调用：仅当裸机 + 配置成本地向量化时，开机就把子进程拉起来。
    Docker / 云端向量化 → 不动（裸机才有「OB 托管 ollama」一说）。"""
    try:
        if sh.in_docker():
            return
        emb = (sh.config.get("embedding") or {})
        fmt = (emb.get("api_format") or "").strip().lower()
        if not emb.get("enabled", True) or fmt not in ("ollama", "local"):
            return
        if not find_ollama_bin():
            return
        res = await ensure_child()
        logger.info(f"[ollama] boot ensure_child: {res}")
    except Exception as e:
        logger.warning(f"[ollama] ensure_child_on_boot failed: {e}")


# ============================================================
# 路由
# ============================================================

def register(mcp) -> None:

    @mcp.custom_route("/api/embedding/local/env", methods=["GET"])
    async def api_local_env(request: Request) -> Response:
        """检测宿主：os/arch/docker/已装/在跑 + 推荐下一步。前端据此渲染面板。"""
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse(await _detect())

    @mcp.custom_route("/api/embedding/local/install", methods=["POST"])
    async def api_local_install(request: Request) -> Response:
        """免提权自动安装 ollama 运行时（后台线程）。body: {mirror?: official|github|<自定义URL前缀>}。"""
        err = sh._require_auth(request)
        if err:
            return err
        global _install_thread, _install_state
        if sh.in_docker():
            return JSONResponse({
                "ok": False,
                "error": "检测到运行在 Docker 容器里：容器内不能给宿主装 ollama。请用 deploy/docker-compose.yml 的 ollama 服务（docker compose up -d ollama）。",
            }, status_code=400)
        if _install_state.get("running"):
            return JSONResponse({"ok": False, "error": "已有安装任务在进行中。"}, status_code=409)
        if find_ollama_bin():
            return JSONResponse({"ok": True, "already": True, "msg": "ollama 已安装，可直接启动。"})
        try:
            body = await request.json()
        except Exception:
            body = {}
        mirror = (str(body.get("mirror") or "official")).strip()
        osk, arch = _os_key(), _arch()
        _install_state = {"running": True, "phase": "starting", "percent": 0.0,
                          "msg": "准备安装…", "error": "", "hint": ""}
        _install_thread = threading.Thread(target=_install_run, args=(osk, arch, mirror), daemon=True)
        _install_thread.start()
        return JSONResponse({"ok": True, "status_path": "/api/embedding/local/install/status"}, status_code=202)

    @mcp.custom_route("/api/embedding/local/install/status", methods=["GET"])
    async def api_local_install_status(request: Request) -> Response:
        err = sh._require_auth(request)
        if err:
            return err
        return JSONResponse({"ok": True, "install": _install_state})

    @mcp.custom_route("/api/embedding/local/start", methods=["POST"])
    async def api_local_start(request: Request) -> Response:
        """把 ollama 作为 OB 子进程拉起来并等就绪。返回是否在跑。"""
        err = sh._require_auth(request)
        if err:
            return err
        res = await ensure_child()
        ok = bool(res.get("running"))
        out = {"ok": ok, **res}
        if not ok and res.get("reason") == "not_installed":
            out["error"] = "ollama 尚未安装，请先点「安装」。"
        elif not ok and res.get("reason") == "in_docker":
            out["error"] = "Docker 环境请用 compose 的 ollama 服务。"
        elif not ok:
            out["error"] = f"启动失败：{res.get('reason')}"
        return JSONResponse(out, status_code=200 if ok else 400)

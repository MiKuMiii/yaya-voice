# -*- coding: utf-8 -*-
"""语音枢纽看门狗：按依赖顺序拉起 9880/9884/9883/8778/8780，缺谁补谁。

设计要点
- 幂等：每个服务先探活，活着就不动；只在不通时动手。
- 顺序：9880(TTS) -> 9884(VAD) -> 9883(ASR) -> 8778(relay) -> 8780(edge)，
  后面的必须等前面 ready 才起，避免 8780 对外报好但下游没起来。
- 僵尸：端口在听但健康检查不过（Modern Standby 之后常见），先杀掉再起。
- 防重叠：文件锁，冷启动要几分钟，期间计划任务的下一轮会直接退出。
- 日志：hub.log 超过 5MB 就轮转，留 3 份。
"""

import json
import msvcrt
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCK = HERE / "hub.lock"
LOG = HERE / "hub.log"

def _env(name, default=""):
    return os.environ.get(name, default)


ROOT = HERE.parent                      # 仓库根
PYW = _env("HUB_PYW") or sys.executable  # 建议指向虚拟环境的 pythonw.exe


def _local(env_name, rel):
    """仓库内脚本：环境变量优先；文件不存在就当这个服务不归我看管。"""
    p = _env(env_name) or str(ROOT / rel)
    return p if Path(p).exists() else ""


def _svc(name, port, script, exe, ready, wait, args=None, cwd=None, extra_args=()):
    if not script:
        return None
    a = list(args) if args else ["-u", script]
    a += list(extra_args)
    return dict(name=name, port=port, marker=os.path.basename(script), exe=exe,
                args=a, cwd=cwd or os.path.dirname(script), ready=ready, wait=wait)


def build_services():
    """默认只托管本仓库的三个服务（vad / relay / edge / mail）；
    TTS 与 ASR 依赖外部模型工程，用环境变量指过来才托管。"""
    out = []

    tts_py = _env("HUB_TTS_PY")
    tts_dir = _env("HUB_TTS_DIR")
    if tts_py and tts_dir and Path(tts_dir, "api_v2.py").exists():
        out.append(dict(
            name="tts", port=9880, marker="api_v2.py", exe=tts_py,
            args=["-u", "api_v2.py", "-a", "0.0.0.0", "-p", "9880",
                  "-c", _env("HUB_TTS_CFG", "GPT_SoVITS/configs/tts_infer.yaml")],
            cwd=tts_dir, ready="tcp", wait=300,
        ))

    out.append(_svc("vad", 9884, _local("HUB_VAD", "vad/vad_server.py"), PYW,
                    "http:http://127.0.0.1:9884/health", 150))

    asr_py = _env("HUB_ASR_PY")
    asr_script = _local("HUB_ASR_SCRIPT", "")
    if asr_script and asr_py:
        out.append(_svc("asr", 9883, asr_script, asr_py,
                        "http:http://127.0.0.1:9883/health", 300))

    out.append(_svc("relay", 8778, _local("HUB_RELAY", "edge/relay.py"), PYW,
                    "tcp", 90,
                    # relay.py 是裸 TCP 转发，没有自己的 /health：8778 上的 /health
                    # 会被原样转给对面，对面一停机就变成「健康不过」。所以只认 TCP。
                    ))

    out.append(_svc("edge", 8780, _local("HUB_EDGE", "edge/voice_edge.py"), PYW,
                    "http:http://127.0.0.1:8780/health", 240))

    out.append(_svc("mail", 8790, _local("HUB_MAILBOX", "hub/mailbox.py"), PYW,
                    "http:http://127.0.0.1:8790/mail/health", 60))

    return [x for x in out if x]


SERVICES = build_services()


def rotate(path: Path, max_bytes=5 * 1024 * 1024, keep=3):
    try:
        if not path.exists() or path.stat().st_size < max_bytes:
            return
        for i in range(keep - 1, 0, -1):
            src = path.with_suffix(path.suffix + ".%d" % i)
            dst = path.with_suffix(path.suffix + ".%d" % (i + 1))
            if src.exists():
                os.replace(src, dst)
        os.replace(path, path.with_suffix(path.suffix + ".1"))
    except Exception:
        pass


def log(msg: str):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def tcp_ok(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.5) as r:
            return r.status == 200
    except Exception:
        return False


def edge_ok() -> bool:
    """8780 要连本地三件（vad/asr/tts）都真就绪、垫音也生成好了才算好。
    brain 是隔壁那台，它挂了不该让我们这边反复重启。
    超时给到 6s：/health 里要探三个本地服务，2.5s 太紧会把好进程判死。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8780/health", timeout=6.0) as r:
            if r.status != 200:
                return False
            import json
            d = json.loads(r.read().decode("utf-8"))
        return bool(d.get("vad") and d.get("asr") and d.get("tts") and d.get("filler"))
    except Exception:
        return False


def edge_busy() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8780/voice_edge/turns", timeout=2.0) as r:
            d = json.loads(r.read().decode("utf-8"))
        return any(s.get("busy") for s in d.get("sessions", []))
    except Exception:
        return False


def tts_alive() -> bool:
    """9880 只探端口是骗人的：模型挂了端口还在。这里真合成一个「嗯」。
    正在对话时跳过，别插队拖慢用户那一轮。"""
    if not http_ok("http://127.0.0.1:9880/docs"):
        return False
    if edge_busy():
        return True
    body = {
        "text": "嗯", "text_lang": "zh",
        "ref_audio_path": os.environ.get("VOICE_REF", ""),
        "prompt_text": "加快拱北口岸改扩建和横琴口岸客货车通道重建。", "prompt_lang": "zh",
        "media_type": "wav", "streaming_mode": 0, "speed_factor": 1.0, "seed": 42,
    }
    req = urllib.request.Request(
        "http://127.0.0.1:9880/tts", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200 and len(r.read()) > 2000
    except Exception:
        return False


def ready(svc) -> bool:
    if svc["name"] == "tts":
        return tts_alive()
    if svc["ready"] == "tcp":
        return tcp_ok(svc["port"])
    if svc["name"] == "edge":
        return edge_ok()
    return http_ok(svc["ready"].split(":", 1)[1])


def port_pid(port: int):
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         encoding="mbcs", errors="replace").stdout
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[1].endswith(":%d" % port) and parts[3] == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def pids_by_marker(marker: str):
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" | "
           "Where-Object { $_.CommandLine -like '*%s*' } | "
           "ForEach-Object { $_.ProcessId }" % marker)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=30)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def kill_tree(pid: int, why: str):
    log("kill %d (%s)" % (pid, why))
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, text=True)


def spawn(svc) -> bool:
    try:
        subprocess.Popen(
            [svc["exe"]] + svc["args"], cwd=svc["cwd"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        return True
    except Exception as e:
        log("%s spawn failed: %s" % (svc["name"], e))
        return False


def wait_ready(svc, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if ready(svc):
            return True
        time.sleep(2.0)
    return False


def apply_requests():
    """restart.txt 里写服务名（tts/vad/asr/relay/edge，可多行）就重启它。
    运维用：不用提权也能让以 SYSTEM 身份跑的服务重启。"""
    req = HERE / "restart.txt"
    if not req.exists():
        return []
    try:
        names = [w.strip() for w in req.read_text(encoding="utf-8-sig").split() if w.strip()]
    except Exception as e:
        log("restart.txt 读失败：%s" % e)
        names = []
    done = []
    for n in names:
        svc = next((s for s in SERVICES if s["name"] == n), None)
        if not svc:
            log("restart.txt: 未知服务 %r" % n)
            continue
        pid = port_pid(svc["port"])
        if pid:
            kill_tree(pid, "restart requested")
        for q in pids_by_marker(svc["marker"]):
            kill_tree(q, "restart requested")
        time.sleep(1.5)
        done.append(n)
    try:
        req.unlink()
    except Exception:
        pass
    if done:
        log("restart requested: %s" % done)
    return done


_fails = {}          # 服务名 -> 连续不健康次数（2 次才杀，防单次抖动误杀）


def prepare(svc) -> str:
    """活着就 'ok'；不在就清掉残留再拉起来，返回 'spawned' / 'spawn-failed'。"""
    name, port = svc["name"], svc["port"]
    if ready(svc):
        _fails.pop(name, None)
        return "ok"
    if tcp_ok(port):
        # 端口有人听但不健康：可能是睡眠唤醒后的僵尸，也可能是探活自己抖了一下
        n = _fails.get(name, 0) + 1
        _fails[name] = n
        if n < 2:
            log("%s 暂时不健康（第 %d 次），先观察" % (name, n))
            return "degraded"
        _fails.pop(name, None)
        pid = port_pid(port)
        if pid:
            kill_tree(pid, "%s unhealthy on %d (x%d)" % (name, port, n))
        time.sleep(1.5)
    else:
        stale = pids_by_marker(svc["marker"])
        for pid in stale:
            kill_tree(pid, "%s stale process (not listening)" % name)
        if stale:
            time.sleep(1.5)

    log("%s starting (port %d)" % (name, port))
    return "spawned" if spawn(svc) else "spawn-failed"


# 分组：同组互不依赖，可以一起起；edge 依赖 TTS/VAD/ASR，必须等前面那组全绿
_BY_NAME = {s["name"] for s in SERVICES}
GROUPS = [g for g in (
    tuple(n for n in ("tts", "vad", "asr", "relay", "mail") if n in _BY_NAME),
    tuple(n for n in ("edge",) if n in _BY_NAME),
) if g]


def main() -> int:
    rotate(LOG)
    lf = open(LOCK, "a+")
    try:
        msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log("another hub instance is running -> exit")
        return 0
    try:
        log("hub tick start (pid %d)" % os.getpid())
        try:
            apply_requests()
        except Exception as e:
            log("apply_requests 失败：%s" % e)

        results = {}
        failed = False
        for group in GROUPS:
            waiting = []
            for name in group:
                svc = next((s for s in SERVICES if s["name"] == name), None)
                if svc is None:
                    continue
                try:
                    st = prepare(svc)
                    results[name] = st
                except Exception as e:
                    results[name] = "error: %s" % e
                    failed = True
                    continue
                if st == "spawned":
                    waiting.append(svc)
                elif st == "ok":
                    results[name] = "ok"
                elif st == "degraded":
                    results[name] = "degraded"      # 留着不动，下一轮再看
                else:
                    failed = True
            for svc in waiting:
                if wait_ready(svc, svc["wait"]):
                    log("%s ready" % svc["name"])
                    results[svc["name"]] = "started"
                else:
                    log("%s NOT ready after %ds" % (svc["name"], svc["wait"]))
                    results[svc["name"]] = "timeout"
                    failed = True
            if failed:
                log("stop cascade after group %s: %s" % (list(group), results))
                break
        log("hub tick done: %s" % results)
        return 0
    finally:
        try:
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        lf.close()


if __name__ == "__main__":
    sys.exit(main())

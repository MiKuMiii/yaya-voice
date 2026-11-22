# -*- coding: utf-8 -*-
"""雅雅局域网中转（纯标准库，不装依赖）

作用：把「手机 -> 这台机器」的流量，转给「那台笔记本:8777」。
手机只要连 这台机器IP:8778，就等于连到了那台笔记本的聊天台（WebView 硬编码走这里）。

11-21 改：后端地址不再是固定值。隔壁那台在 ICS 段里 DHCP，地址天天变
（每次开机都可能变），所以这里自己做发现：连不上就扫 x.x.x.2-254:8777，
用 `GET /mail/health` 认人（返回里 me=电脑 才是它）。缓存最多 5 分钟。

用法： python relay.py                      （默认读同目录 relay.json）
       python relay.py --listen 8778 --target 192.168.137.10:8777
       python relay.py --no-discover        （只用 cfg 里的 target，不扫）
"""
import argparse
import json
import os
import re
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "relay.log")
_lock = threading.Lock()

TARGET_PORT = 8777
ICS_PREFIX = os.environ.get("RELAY_ICS_PREFIX", "192.168.137.")
CACHE_MAX_S = 300.0     # 缓存最多 5 分钟，超过就重扫（别在失败后还死磕旧 IP）
PROBE_TIMEOUT = 0.35


def log(msg):
    line = time.strftime("%m-%d %H:%M:%S ") + msg
    with _lock:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _http_health(host, port, path="/mail/health", timeout=PROBE_TIMEOUT):
    """手写一个极简 HTTP GET（不引 requests / 不依赖 py 版本），返回 body 或 None。"""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception:
        return None
    try:
        s.settimeout(timeout)
        req = ("GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, host)).encode()
        s.sendall(req)
        buf = b""
        while True:
            d = s.recv(4096)
            if not d:
                break
            buf += d
            if len(buf) > 8192:
                break
        if not buf.startswith(b"HTTP/1."):
            return None
        if b" 200 " not in buf.split(b"\r\n", 1)[0]:
            return None
        head, _, body = buf.partition(b"\r\n\r\n")
        # 可能是 chunked，粗暴处理：去掉块长度行
        if b"chunked" in head.lower():
            parts = []
            for line in body.split(b"\r\n"):
                if re.fullmatch(rb"[0-9a-fA-F]+", line.strip() or b"x"):
                    continue
                parts.append(line)
            body = b"".join(parts)
        return body
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass


def looks_like_peer(host, port=TARGET_PORT):
    """认人：隔壁的 /mail/health 里有 me=电脑。别的机器上刚好开 8777 也不会误认。"""
    body = _http_health(host, port)
    if not body:
        return False
    try:
        j = json.loads(body.decode("utf-8", "ignore"))
    except Exception:
        return False
    return bool(j.get("ok")) and ("电脑" in str(j.get("me", "")) or "key_set" in j)


def scan(prefix=ICS_PREFIX, port=TARGET_PORT):
    hosts = ["%s%d" % (prefix, i) for i in range(2, 255)]
    found = []
    with ThreadPoolExecutor(max_workers=64) as ex:
        for h, ok in zip(hosts, ex.map(lambda x: looks_like_peer(x, port), hosts)):
            if ok:
                found.append(h)
                break       # 命中一个就够了，别把 254 个都扫完
    return found


class Backend(object):
    """当前后端地址 + 发现逻辑。失败时立刻置脏，下一次连接触发重扫。"""

    def __init__(self, target, discover=True):
        host, _, port = target.rpartition(":")
        self.host = host or target
        self.port = int(port or TARGET_PORT)
        self.discover = discover
        self.ts = 0.0           # 上次确认/发现的时间
        self.lock = threading.Lock()

    def addr(self):
        return (self.host, self.port)

    def mark_bad(self):
        with self.lock:
            self.ts = 0.0

    def refresh(self, why=""):
        if not self.discover:
            return
        with self.lock:
            if self.ts and time.time() - self.ts < CACHE_MAX_S:
                return
            t0 = time.time()
            # 先试当前地址，再扫
            if looks_like_peer(self.host, self.port):
                self.ts = time.time()
                log("relay 命中：%s:%d（沿用，探活 %.0fms）%s"
                    % (self.host, self.port, (time.time() - t0) * 1000, why))
                return
            found = scan()
            if found:
                old = self.host
                self.host = found[0]
                self.ts = time.time()
                log("relay 命中：%s:%d（原 %s，扫描 %.1fs）%s"
                    % (self.host, self.port, old, time.time() - t0, why))
            else:
                log("relay 找不到后端（原 %s:%d，扫描 %.1fs）%s"
                    % (self.host, self.port, time.time() - t0, why))

    def watcher(self, every=60.0):
        while True:
            time.sleep(every)
            try:
                self.refresh("(定时)")
            except Exception as e:
                log("relay 定时刷新出错：%s" % e)


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def handle(client, addr, backend):
    backend.refresh("")                 # 次数不多：缓存内直接返回
    target_host, target_port = backend.addr()
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        up = socket.create_connection((target_host, target_port), timeout=10)
    except Exception as e:
        log("连接后端失败 %s:%s (%s)" % (target_host, target_port, e))
        backend.mark_bad()
        try:
            client.close()
        except Exception:
            pass
        return
    try:
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception:
        pass
    t1 = threading.Thread(target=pipe, args=(client, up), daemon=True)
    t2 = threading.Thread(target=pipe, args=(up, client), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for s in (client, up):
        try:
            s.close()
        except Exception:
            pass


def serve(listen_port, backend):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(64)
    log("中转已启动：0.0.0.0:%d  ->  %s:%d（自适应=%s）"
        % (listen_port, backend.host, backend.port, backend.discover))
    while True:
        try:
            client, addr = srv.accept()
        except Exception as e:
            log("accept 出错：%s" % e)
            continue
        log("连接来自 %s:%s" % (addr[0], addr[1]))
        threading.Thread(target=handle, args=(client, addr, backend), daemon=True).start()


def main():
    cfg = {"listen": 8778, "target": os.environ.get("RELAY_TARGET", ""), "discover": True}
    path = os.path.join(HERE, "relay.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log("relay.json 读不了，用默认值：%s" % e)
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int)
    ap.add_argument("--target")
    ap.add_argument("--no-discover", action="store_true")
    a = ap.parse_args()
    if a.listen:
        cfg["listen"] = a.listen
    if a.target:
        cfg["target"] = a.target
    if a.no_discover:
        cfg["discover"] = False
    be = Backend(cfg["target"], discover=bool(cfg.get("discover", True)))
    be.refresh("(启动)")
    threading.Thread(target=be.watcher, daemon=True).start()
    serve(int(cfg["listen"]), be)


if __name__ == "__main__":
    main()

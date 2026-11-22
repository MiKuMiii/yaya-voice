#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前自查：扫仓库里有没有命不该出现在公开仓库里的东西。

用法：python sanitize.py <要扫的目录>
命中就非零退出，并把文件:行号打出来。
"""
import os
import re
import sys

RULES = [
    ("DeepSeek key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("智谱/BigModel key", re.compile(r"[0-9a-f]{32}\.[A-Za-z0-9]{16,}")),
    ("通用 secret 赋值", re.compile(r"(?i)(api[_-]?key|token|secret|passwd|password)\s*[:=]\s*['\"][^'\"]{12,}")),
    ("内网 IP", re.compile(r"\b(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")),
    ("tailnet 域名", re.compile(r"[a-z0-9-]+\.ts\.net")),
    ("本机绝对路径（Windows 家目录）", re.compile(r"[A-Za-z]:\\\\?家\\\\?|C:\\\\Users\\\\?[A-Za-z0-9_.-]+\\\\")),
    ("QQ 号（9-11 位纯数字单独成词）", re.compile(r"(?<!\d)\d{9,11}(?!\d)")),
]

SKIP_DIRS = {".git", "node_modules", "__pycache__", "build", "dist", "venv", ".venv"}

# 文档里的占位/通用值，不算泄露：
#   · 192.168.137.10  示例地址，谁都可以是它
#   · 192.168.137.0/24 是 Windows「Internet 连接共享」的固定网段，属于公开常识
ALLOW = ("192.168.137.10", "192.168.137.0/24", "192.168.1.0/24")
SKIP_EXT = {".wav", ".mp3", ".ogg", ".png", ".jpg", ".jpeg", ".webp", ".zip", ".pth", ".index", ".onnx"}

# 这些是「不该出现在公开仓库里」的二进制：权重、音频、样本、安装包
FORBIDDEN_EXT = {
    ".pth", ".onnx", ".ckpt", ".safetensors", ".index", ".pt", ".bin",
    ".wav", ".mp3", ".flac", ".m4a", ".ogg",
    ".apk", ".aab", ".zip",
}


def scan(root):
    hits = []
    forbidden = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in FORBIDDEN_EXT:
                forbidden.append(os.path.join(base, fn))
                continue
            if ext in SKIP_EXT:
                continue
            p = os.path.join(base, fn)
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if any(a in line for a in ALLOW):
                    continue
                for name, rx in RULES:
                    if rx.search(line):
                        hits.append((p, i, name, line.strip()[:120]))
    return hits, forbidden


def main():
    if len(sys.argv) < 2:
        print("用法: python sanitize.py <目录>")
        return 2
    hits, forbidden = scan(sys.argv[1])
    bad = 0
    if forbidden:
        bad += len(forbidden)
        print("仓库里混进了 %d 个不该分发的二进制（权重/音频/安装包），请删掉或加进 .gitignore：" % len(forbidden))
        for p in forbidden:
            print("  禁入文件  %s" % p)
    if hits:
        bad += len(hits)
        print("扫到 %d 处可能泄露的信息：" % len(hits))
        for p, i, name, snippet in hits:
            print("  %s:%d  [%s]  %s" % (p, i, name, snippet))
    if not bad:
        print("干净：没有禁入二进制，也没扫到 key / 内网 IP / tailnet / 本机路径 / QQ 号。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

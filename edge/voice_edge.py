"""语音枢纽 /voice_edge —— 8780
手机直连入口：VAD + ASR + TTS 都在本机，只把「该回什么」问另一台「大脑机」（地址见 VOICE_BRAIN）。
无状态：音频缓冲在 9884，人格/记忆/历史在隔壁。本服务只管一轮问答的搬运。

依赖本机已在跑的服务：
  9884  /vad_stream?sid=   （流式 VAD，end 时带 text）
  9880  /tts               （GPT-SoVITS，media_type=raw + streaming_mode=3 拿到 ~0.3s 首块）
隔壁：
  8777  /api/voice/brain/stream?text=&sid=   （SSE，逐句吐）
"""
import base64
import io
import json
import logging
import logging.handlers
import os
import sys
import asyncio
import threading
import time
import wave
from collections import deque

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))

VAD = os.environ.get("VOICE_VAD", "http://127.0.0.1:9884")
TTS = os.environ.get("VOICE_TTS", "http://127.0.0.1:9880")
BRAIN = os.environ.get("VOICE_BRAIN", "")          # 大脑基址；空则只靠扫描/备用列表发现
ASR = os.environ.get("VOICE_ASR", "http://127.0.0.1:9883")

# 参考音是「一句话的干声」（实测 5.5s 最稳）。要求：
#   · 3~10 秒、单人、无伴奏无混响
#   · 与 VOICE_PROMPT 指向的文本**逐字对应**，否则合成会念崩
#   · 用了谁的声音就是你自己的事：公开分发他人音色（含虚拟歌手）有法律风险，见 CREDITS.md
REF_WAV = os.environ.get("VOICE_REF", "")          # TTS 参考音（3~10s 干声）；空则 /tts 必 502
PROMPT_TXT = os.environ.get("VOICE_PROMPT", "")    # 与参考音逐字对应的文本文件
PROMPT_LANG = os.environ.get("VOICE_PROMPT_LANG", "zh")
FILLER_TEXT = os.environ.get("VOICE_FILLER_TEXT", "嗯……让我想想啊。")

TTS_MODE = 3          # 3 = fixed_length_chunk（首块实测 0.29~0.44s）
TTS_TOP_K, TTS_TOP_P, TTS_TEMP = 5, 1.0, 1.0
TTS_SPEED = 1.0        # 主人 11-17 定 1.0：说话版模型 1.0 就已经是 0.24 s/字（旧模型 1.8 才 0.28）
SEG_PCM_SEC = 0.9     # 攒够这么多秒的音频就作为一段推出去

# —— 出音质量（2026-11-18 隔壁提「卡顿 + 电流音」后加的）——
# 1) 切点尽量落在低能量处：每 0.9s 不是硬切，而是在 ±150ms 里挑最安静的一个 10ms 再切，
#    避免切在元音中间，也避免切出一整块静音。
CUT_SEARCH_MS = 150
# 2) 整段近乎无声的直接吞掉：按句合成时每句自带首尾静音，0.9s 切块很容易切出整块空白，
#    手机上没法区分「真静音」和「网络还没到」，连播就是句子中间掉一下。
SILENT_PEAK = 300
# 3) 段首尾淡入淡出，防那一下「咔」被听成断点。切点已经挑在静音处，所以只做 3ms。
FADE_MS = 3
# 4) 低通：VOICE_LP=12000 之类才开，默认关（我们自己的字节实测 7kHz 以上只占 1.5%，先不动）
LP_CUTOFF = int(os.environ.get("VOICE_LP", "0") or 0)
# 5) 取证：VOICE_DUMP=<目录> 时，把每个真正发出去的段原样落盘（隔壁要求的「base64 之前最后一刻」）
DUMP_DIR = os.environ.get("VOICE_DUMP", "")
TTS_TIMEOUT = 120
BRAIN_CONNECT_TIMEOUT = 4
BRAIN_FIRST_BYTE_TIMEOUT = 6
BRAIN_TOTAL_TIMEOUT = 30
PACKET_TIMEOUT = 3
AWAKE_KEEP_S = 30.0   # 一轮结束后保持「醒着」的时长，便于连续对话
SESSION_IDLE_S = 120.0
# 回完话之后再这么久没有新包，就把会话退回 idle（别挂在 thinking / dropped 上）
IDLE_RESET_S = 20.0
RESUME_AFTER_UNDO_S = float(os.environ.get("VOICE_RESUME_S", "2.0"))  # 悔棋后多久没新声就按已说的收尾
# end 之后再压这么久才真回话：这期间又出现人声就作废这一轮（隔壁 11-18 要求的「悔棋」）
TURN_GRACE_MS = int(os.environ.get("VOICE_GRACE", "700"))
# 每段回复的播放预算（毫秒），手机拿它判断要不要抢话打断
REPLY_BUDGET_MS = 4000
# 首句最长字数：一上来就长句，手机那头听着像在念稿；超过就按标点切成两截
FIRST_PIECE_MAX = 25
# 只有这些词自己收尾成一轮时，不当作一句话（去问大脑会拿到空回复，然后回垫音把手机麦克风关掉）
WAKE_WORDS = ("嘿雅雅", "雅雅", "呀呀", "耶耶", "喂雅雅")
REPLY_WAIT_MS = 3000  # filler 没开时，POST 最多等这么久拿到第一段 reply

# ---- KWS（唤醒词判定搬到本机：参数是隔壁 11-14 在真机上调出来的，别自己再调）
KWS_MODELS = {          # 名字 -> (onnx 路径, 阈值)；阈值可用环境变量临时压/放，别改代码
    "yaya_v1": (os.environ.get("VOICE_KWS_V1", ""), float(os.environ.get("VOICE_KWS_TH_V1", "0.93"))),
    # 11-21 用留证 wav 核过（kws_yaya_v1_20261121-010313）：主人真喊「雅雅」时
    # yaya_v1 = 0.997，yaya_hey_v1 = 0.0003 —— hey 对真唤醒词根本没反应；
    # 反而 20:24 那句「你们好你们好」让 hey 过了 0.995（假触发）。
    # 所以 hey 阈值放回 0.99（等于只留个兜底，别让它假醒），主力靠 v1。
    "yaya_hey_v1": (os.environ.get("VOICE_KWS_HEY", ""), float(os.environ.get("VOICE_KWS_TH_HEY", "0.99"))),
}
KWS_NEED_FRAMES = int(os.environ.get("VOICE_KWS_FRAMES", "2"))   # 窗内至少几帧过阈值
KWS_FRAME = 1280        # 80ms @16k，openWakeWord 的固定帧长
KWS_HIT_WINDOW_S = 0.8  # 800ms 内出现两帧过阈值才算唤醒（单帧容易误杀）
KWS_PEAK_MIN = 1200     # 触发那一包的 PCM16 绝对值峰值下限（11-21 误唤醒里有一条 peak=841，抬到 1200）
KWS_ASR_CONFIRM = os.environ.get("VOICE_KWS_ASR", "1") != "0"
# ASR 认出来的「雅雅」变体（9883 常把雅雅写成丫丫/呀呀）
KWS_ASR_VARIANTS = ("雅雅", "丫丫", "呀呀", "亚亚", "牙牙", "吖吖", "雅亚", "押押", "鸦鸦",
                    "嘿雅雅", "喂雅雅", "那个雅雅")
# 音量门限看多长一段窗里的峰值：模型打分滞后 0.4~0.9s（大包 0.5s 一包时更明显），
# 用 800ms 会把真唤醒判成「quiet」（11-18 用真机节奏 0.5s 一包实测踩到过）
KWS_PEAK_WINDOW_S = 2.0
KWS_COOLDOWN_S = 3.0    # 唤醒后 3 秒内不再判唤醒
# 没给模型路径就等于没开唤醒（只认客户端自己判定的 force）
KWS_ENABLED = os.environ.get("VOICE_KWS", "1") != "0" and any(p for p, _ in KWS_MODELS.values())
PRE_ROLL_PKTS = 4       # 唤醒前留几包（2s@0.5s）：KWS 打分滞后，唤醒那一刻把开头补回 VAD

# KWS 命中留证：把 [命中前 2s, 命中后 1s] 存成 wav 丢进 /dl，隔壁拿同一份文件核对阈值
# （11-21 隔壁要的：手机只往 8780 打音频，它那边截不到这一段）
KWS_DUMP_PRE_S = 2.0
KWS_DUMP_POST_S = 1.0
KWS_DUMP_DIR = os.environ.get("VOICE_DUMP_DIR", "")  # 唤醒留证 wav 落盘目录；空则不落
KWS_DUMP_ON = os.environ.get("VOICE_KWS_DUMP", "1") != "0" and bool(KWS_DUMP_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(
                  os.path.join(HERE, "voice_edge.log"), maxBytes=20 * 1024 * 1024,
                  backupCount=3, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("edge")

app = FastAPI()
_lock = threading.Lock()
_sessions = {}


# ---------------------------------------------------------------- 音频小工具
def wav_bytes(pcm: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.astype("<i2").tobytes())
    return buf.getvalue()


def b64_wav(pcm: np.ndarray, sr: int) -> str:
    return base64.b64encode(wav_bytes(pcm, sr)).decode("ascii")


_prompt_cache = {}


def prompt_text() -> str:
    if "p" not in _prompt_cache:
        try:
            _prompt_cache["p"] = open(PROMPT_TXT, encoding="utf-8-sig").read().strip()
        except Exception:
            # 没有 prompt 文本文件时留空：GPT-SoVITS 会退化成「无提示」合成，
            # 音色会漂，但至少不崩。正式部署请务必配上 VOICE_PROMPT。
            _prompt_cache["p"] = ""
    return _prompt_cache["p"]


LANGS = ("zh", "en", "ja", "ko", "yue", "auto", "all_zh", "all_ja")

# 主人要求「雅雅」念第二声（yá yá）。汉字「雅」是第三声，模型念出来是下坠调，
# 听着发闷；换成同音的「牙」就是第二声，字幕照旧写「雅雅」。
# 只改送进 TTS 的文本，不改任何对外字段。VOICE_NAME_SAY=off 可关掉。
NAME_SAY = os.environ.get("VOICE_NAME_SAY", "")  # 空=不改字：实测「雅雅」本字念得对，改写成「牙牙」反而听成牙牙
NAME_WRITE = "雅雅"


def say(text: str) -> str:
    """把要念出来的文本里的名字换成第二声写法。"""
    if not NAME_SAY or NAME_SAY == NAME_WRITE:
        return text
    return text.replace(NAME_WRITE, NAME_SAY)


def tts_params(text: str) -> dict:
    return dict(text=say(text), text_lang="zh", ref_audio_path=REF_WAV, prompt_text=prompt_text(),
                prompt_lang=PROMPT_LANG, top_k=TTS_TOP_K, top_p=TTS_TOP_P, temperature=TTS_TEMP,
                text_split_method="cut5", batch_size=1, speed_factor=TTS_SPEED,
                media_type="raw", streaming_mode=TTS_MODE, seed=42)


class _LowPass:
    """带状态的 4 阶低通：跨段连续滤波，不会在段边界留台阶。"""

    def __init__(self, cutoff, sr=32000):
        self.sos = None
        self.zi = None
        if cutoff and cutoff > 1000:
            from scipy import signal
            self.sos = signal.butter(4, cutoff, "lowpass", fs=sr, output="sos")

    def __call__(self, pcm):
        if self.sos is None:
            return pcm
        from scipy import signal
        x = pcm.astype(np.float64)
        if self.zi is None:
            self.zi = signal.sosfilt_zi(self.sos) * (x[0] if x.size else 0.0)
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def _peak(pcm) -> int:
    return int(np.abs(pcm.astype(np.int32)).max()) if pcm.size else 0


def _fade(pcm, n):
    """首尾各 n 个样本线性淡入淡出。"""
    if n <= 0 or pcm.size <= 2 * n:
        return pcm
    out = pcm.copy()
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[:n] = (out[:n].astype(np.float32) * ramp).astype(np.int16)
    out[-n:] = (out[-n:].astype(np.float32) * ramp[::-1]).astype(np.int16)
    return out


def _cut_point(buf, want: int, search: int, sr: int = 32000) -> int:
    """在 [want-search, want+search] 里找最安静的 10ms 窗，返回切点。"""
    step = max(int(0.01 * sr), 2)
    lo = max(want - search, step)
    hi = min(want + search, buf.size - step)
    if hi <= lo:
        return min(want, buf.size)
    best, best_v = want, None
    for i in range(lo, hi + 1, max(step // 2, 1)):
        v = int(np.abs(buf[i:i + step]).max())
        if best_v is None or v < best_v:
            best, best_v = i, v
    return best


def dump_seg(pcm, sr=32000):
    """把「塞进 audio_b64 之前」的字节落盘，并记两种高频口径（幅值占比 / 功率占比）。"""
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        n = len([f for f in os.listdir(DUMP_DIR) if f.startswith("seg") and f.endswith(".wav")])
        path = os.path.join(DUMP_DIR, "seg%02d.wav" % n)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        x = pcm.astype(np.float64)
        win = np.hanning(2048)
        amp_hi = amp_all = 0.0
        ps = np.zeros(1025)
        c = 0
        for i in range(0, max(x.size - 2048, 1), 1024):
            a = np.abs(np.fft.rfft(x[i:i + 2048] * win))
            amp_hi += a[448:].sum()
            amp_all += a.sum()
            ps += a ** 2
            c += 1
        f = np.fft.rfftfreq(2048, 1.0 / sr)
        pw = ps[f >= 7000].sum() / ps.sum() * 100 if ps.sum() else 0.0
        log.info("dump %s n=%d peak=%d hf_amp=%.2f%% hf_pow=%.2f%%",
                 os.path.basename(path), pcm.size, _peak(pcm),
                 amp_hi / amp_all * 100 if amp_all else 0.0, pw)
    except Exception as e:
        log.warning("dump failed: %s", e)


def tts_stream(text: str, on_pcm, seg_samples: int):
    """把一段文字念出来，边到边回调。on_pcm(np.int16 一维数组); 返回总样本数。"""
    url = TTS + "/tts"
    sr = None
    first = None
    t0 = time.time()
    n_total = 0
    lp = _LowPass(LP_CUTOFF)
    search = int(CUT_SEARCH_MS / 1000.0 * 32000)
    fade_n = int(FADE_MS / 1000.0 * 32000)
    raw = b""
    buf = np.zeros(0, dtype=np.int16)

    def emit(pcm):
        nonlocal n_total
        if pcm.size == 0:
            return
        pk = _peak(pcm)
        if pk < SILENT_PEAK:
            log.info("drop silent seg peak=%d n=%d", pk, pcm.size)
            return
        out = _fade(lp(pcm), fade_n)
        if DUMP_DIR:
            dump_seg(out)
        on_pcm(out)
        n_total += out.size

    with requests.get(url, params=tts_params(text), stream=True, timeout=TTS_TIMEOUT) as r:
        r.raise_for_status()
        sr = int(r.headers.get("x-sample-rate") or 0) or None
        for chunk in r.iter_content(chunk_size=8192):
            if not chunk:
                continue
            if first is None:
                first = time.time() - t0
                log.info("tts first chunk %.2fs len(text)=%d", first, len(text))
            raw += chunk
            usable = len(raw) - (len(raw) % 2)
            if usable:
                buf = np.concatenate([buf, np.frombuffer(raw[:usable], dtype="<i2")])
                raw = raw[usable:]
            while buf.size >= seg_samples + search:
                cut = _cut_point(buf, seg_samples, search)
                emit(buf[:cut])
                buf = buf[cut:]
    if buf.size:
        emit(buf)
    return n_total, sr, (first or 0.0)


def tts_sr() -> int:
    """问一次 TTS 要个 wav 头，拿到真实采样率（缓存）。"""
    if "sr" in _prompt_cache:
        return _prompt_cache["sr"]
    try:
        p = tts_params("嗯。")
        p.update(media_type="wav", streaming_mode=0)
        r = requests.get(TTS + "/tts", params=p, timeout=TTS_TIMEOUT)
        r.raise_for_status()
        with wave.open(io.BytesIO(r.content), "rb") as w:
            sr = w.getframerate()
    except Exception as e:
        log.warning("tts_sr probe failed: %s", e)
        sr = 32000
    _prompt_cache["sr"] = sr
    log.info("tts sample rate = %d", sr)
    return sr


def tts_wav(text: str, speed=None, fmt="wav", extra=None) -> bytes:
    """给外部（QQ 侧 AstrBot）用的「一句话换一个文件」：默认参考音/文本都在这里配好，
    调用方只要给 text。GET 参数多也无所谓，多余的会被忽略（AstrBot 会带一堆）。

    extra 里如果带了**本机真实存在**的 ref_audio_path，以及配套的 prompt_text /
    prompt_lang，就按它来（方案 B：对面自己指定参考音）；路径不存在就静默用默认，
    绝不因为对面随手填了 edge-tts 的路径而整个 500。"""
    p = tts_params(text)
    p.update(media_type=fmt, streaming_mode=0, text_split_method="cut5")
    if speed:
        try:
            p["speed_factor"] = float(speed)
        except Exception:
            pass
    if extra:
        ref = str(extra.get("ref_audio_path") or "").strip()
        if ref and os.path.isfile(ref):
            p["ref_audio_path"] = ref
            pt = str(extra.get("prompt_text") or "").strip()
            if pt:
                p["prompt_text"] = pt
            lang = str(extra.get("prompt_lang") or "").strip().lower()
            if lang in LANGS:
                p["prompt_lang"] = lang
        elif ref:
            log.info("tts: 忽略不存在的 ref_audio_path=%r，用默认参考音", ref[:80])
        tl = str(extra.get("text_lang") or "").strip().lower()
        if tl in LANGS:
            p["text_lang"] = tl
    r = requests.post(TTS + "/tts", json=p, timeout=TTS_TIMEOUT)
    r.raise_for_status()
    return r.content


def make_filler() -> dict:
    """预生成垫音，常驻内存。"""
    sr = tts_sr()
    frames = []

    def acc(pcm):
        frames.append(pcm)

    try:
        tts_stream(FILLER_TEXT, acc, int(sr * 5))
        pcm = np.concatenate(frames) if frames else np.zeros(0, dtype="<i2")
        dur = pcm.size / float(sr)
        log.info("filler ready: %.2fs, %.1f KB", dur, pcm.nbytes / 1024.0)
        return {"text": FILLER_TEXT, "mime": "audio/wav", "audio_b64": b64_wav(pcm, sr), "sr": sr}
    except Exception as e:
        log.warning("filler 生成失败：%s", e)
        return {}


# ---------------------------------------------------------------- 大脑（SSE）
# 隔壁那台挂在我这台机的 ICS 后面（192.168.137.0/24，网关就是我们），
# 租约一变地址就换（见过 .199、.12），所以不写死地址：先用缓存，不通就扫一遍网段。
ICS_PREFIX = os.environ.get("VOICE_ICS_PREFIX", "192.168.137.")
BRAIN_FALLBACKS = [u.strip() for u in os.environ.get("VOICE_BRAIN_FALLBACKS", "").split(",") if u.strip()]
_brain = {"url": BRAIN, "ts": 0.0, "ok": None, "scan_ts": 0.0, "warn_ts": 0.0}
BRAIN_SCAN_MIN_S = 20.0     # 扫 137.0/24 的最小间隔（不设限的话 /health 每次都会扫 254 个地址）
BRAIN_WARN_MIN_S = 60.0     # 「找不到」这类日志最多一分钟一条


def _brain_probe(url, timeout=0.5):
    try:
        return requests.get(url + "/mail/health", timeout=timeout).status_code == 200
    except Exception:
        return False


def discover_brain(force=False):
    """找到大脑在哪。命中过就缓存 60s，不通再扫 137.0/24。

    注意：扫描有 20 秒最小间隔，且「找不到」日志一分钟最多一条。
    以前这两样都没有，隔壁一关机 /health 每次都扫 254 个地址、外带刷日志，
    直接把 /health 拖到 5 秒以上，看门狗以为 edge 挂了就开始反复重启（11-20 那次）。
    """
    now = time.time()
    cur = _brain["url"]
    if not force and now - _brain["ts"] < 60 and _brain_probe(cur):
        return cur
    for c in [cur, BRAIN] + BRAIN_FALLBACKS:
        if _brain_probe(c):
            if c != cur:
                log.info("brain 换地址：%s -> %s", cur, c)
            _brain["url"], _brain["ts"] = c, now
            return c
    if now - _brain["scan_ts"] < BRAIN_SCAN_MIN_S:
        return cur                       # 刚扫过，别重复扫
    _brain["scan_ts"] = now
    from concurrent.futures import ThreadPoolExecutor
    hosts = ["http://%s%d:8777" % (ICS_PREFIX, i) for i in range(2, 255)]
    try:
        with ThreadPoolExecutor(max_workers=64) as ex:
            for url, ok in zip(hosts, ex.map(lambda u: _brain_probe(u, 0.35), hosts)):
                if ok:
                    log.info("brain 扫描命中：%s（原 %s）", url, cur)
                    _brain["url"], _brain["ts"] = url, now
                    return url
    except Exception as e:
        log.warning("brain 扫描失败：%s", e)
    if now - _brain["warn_ts"] >= BRAIN_WARN_MIN_S:
        _brain["warn_ts"] = now
        log.warning("brain 找不到（原 %s），%d 秒后再试", cur, int(BRAIN_SCAN_MIN_S))
    return cur


def brain_url():
    return _brain["url"]


def brain_watch():
    """后台每 15 秒探一次大脑，只更新缓存。绝不让 /health 去扫网段。"""
    while True:
        try:
            if _brain_probe(_brain["url"], 0.6):
                _brain["ok"] = True
                _brain["ts"] = time.time()
            else:
                url = discover_brain(force=True)
                _brain["ok"] = _brain_probe(url, 0.6)
        except Exception:
            _brain["ok"] = False
        time.sleep(15.0)


def brain_stream(text: str, sid: str):
    """逐句产出隔壁给的 text 片段。连不上就空转返回。"""
    url = brain_url() + "/api/voice/brain/stream"
    t0 = time.time()
    try:
        with requests.get(url, params={"text": text, "sid": sid}, stream=True,
                          timeout=(BRAIN_CONNECT_TIMEOUT, BRAIN_FIRST_BYTE_TIMEOUT)) as r:
            r.raise_for_status()
            for raw in r.iter_lines(decode_unicode=True):
                if time.time() - t0 > BRAIN_TOTAL_TIMEOUT:
                    log.warning("brain total timeout %.0fs, stop", BRAIN_TOTAL_TIMEOUT)
                    break
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    if payload == "[DONE]":
                        break
                    continue
                try:
                    obj = json.loads(payload)
                except Exception:
                    log.warning("brain bad json: %r", payload[:80])
                    continue
                if obj.get("done"):
                    break
                piece = (obj.get("text") or "").strip()
                if piece:
                    yield piece
    except Exception as e:
        log.warning("brain 不可用（%s）：按 done 处理", e)
        _brain["ts"] = 0.0          # 下次调用重新找地址（ICS 租约可能又变了）
        return


# ---------------------------------------------------------------- 会话
class Session:
    def __init__(self, sid):
        self.sid = sid
        self.awake_at = 0.0
        self.events = deque()
        self.last = time.time()
        self.busy = False
        self.n_pkt = 0
        self.n_err = 0
        self.playing = False
        self.n_seq = 0          # 事件序号：喂给 /api/voice/reply 的 token
        self.wake_at = 0.0      # 上次唤醒时刻（KWS 冷却用）
        self.kws = None         # openWakeWord Model，第一次用到时建
        self.kws_buf = np.zeros(0, dtype=np.int16)
        self.kws_hits = []      # [(时刻, 模型名)] 只用最近 800ms
        self.kws_peaks = []     # [(时刻, 该帧峰值)] 音量门限用
        self.pre = deque(maxlen=PRE_ROLL_PKTS)   # 唤醒前的音频，唤醒时补喂 VAD
        self.prefix = ""        # 唤醒时补喂那段自己收尾的文本（通常是「雅雅」）
        self.pending = None     # 判完 end、还在悔棋窗口里的一轮 {"text":..,"t":..}
        self.filler_q = None    # 悔棋窗口过了才生成的垫音，等下一个包带回去
        self.turn_seq = 0       # 第几轮：/voice_edge/end 一按就 +1，跑着的 run_turn 自己发现作废
        self.resume_id = 0      # 悔棋后的兜底定时器编号：一有新人声就 +1，让旧定时器空转
        self.roll = bytearray()  # 最近几秒的原始 PCM，KWS 命中时倒着取 [命中前 2s]
        self.dump = None        # 正在攒的留证 wav {"buf": bytearray, "need": int, "model": str}

    def awake(self):
        return (time.time() - self.awake_at) < AWAKE_KEEP_S

    def push(self, ev):
        ev.setdefault("sid", self.sid)
        with _lock:
            ev["i"] = self.n_seq
            self.n_seq += 1
            ev["next"] = self.n_seq
            self.events.append(ev)
        return ev

    def pop(self):
        with _lock:
            if self.events:
                return self.events.popleft()
        return None

    def clear_events(self):
        with _lock:
            self.events.clear()
            self.n_seq = 0


def get_session(sid) -> Session:
    with _lock:
        s = _sessions.get(sid)
        if s is None:
            s = _sessions[sid] = Session(sid)
        s.last = time.time()
        return s


# ---------------------------------------------------------------- 唤醒词
def new_kws_model():
    from openwakeword.model import Model
    return Model(wakeword_models=[p for p, _ in KWS_MODELS.values()],
                 inference_framework="onnx")


def asr_text_16k(x) -> str:
    """把 16k mono PCM16 直接送 9883 转写（一条 2.5s 的话 20ms 上下）。"""
    try:
        r = requests.post(ASR + "/asr", params={"sr": 16000},
                          data=np.asarray(x, dtype="<i2").tobytes(), timeout=8)
        r.raise_for_status()
        return (r.json() or {}).get("text", "") or ""
    except Exception as e:
        log.warning("asr confirm failed: %s", e)
        return ""


def _wake_asr_ok(sess) -> tuple:
    """KWS 命中后，用 ASR 复核滚动缓冲里最后 2.5s 到底有没有「雅雅」。

    11-21 真机连出四次误唤醒，全部是 KWS 打分 0.99 以上但人说的是别的话：
      「待会我去。」「去哪儿了？」「好了吗？」「哎呀。」
    转写之后一眼可分，所以加这道闸：认不出来（空文本）就放行，认出来但不像唤醒词就否掉。
    """
    buf = bytes(sess.roll)
    if len(buf) < 16000:
        return True, ""
    x = np.frombuffer(buf[-int(2.5 * 16000) * 2:], dtype=np.int16)
    txt = asr_text_16k(x)
    if not txt.strip():
        return True, txt
    return any(v in txt for v in KWS_ASR_VARIANTS), txt


def kws_feed(sess: Session, pcm: bytes):
    """喂一段 16k mono PCM16 给 KWS；命中返回模型名，否则 None。"""
    if not KWS_ENABLED or not pcm:
        return None
    now = time.time()
    if now - sess.wake_at < KWS_COOLDOWN_S:
        return None
    a = np.frombuffer(pcm, dtype=np.int16)
    if a.size == 0:
        return None
    if sess.kws is None:
        t0 = time.time()
        sess.kws = new_kws_model()
        log.info("sid=%s kws model ready (%.0fms)", sess.sid, (time.time() - t0) * 1000)
    buf = np.concatenate([sess.kws_buf, a]) if sess.kws_buf.size else a
    n_full = buf.size // KWS_FRAME
    fresh = []
    peaks = []
    for k in range(n_full):
        frame = buf[k * KWS_FRAME:(k + 1) * KWS_FRAME]
        peaks.append((now, int(np.abs(frame).max())))
        sc = sess.kws.predict(frame)
        for name, v in sc.items():
            if v >= KWS_MODELS.get(name, (None, 1.01))[1]:
                fresh.append((now, name))
    sess.kws_buf = buf[n_full * KWS_FRAME:].copy()
    sess.kws_hits = [h for h in sess.kws_hits if now - h[0] <= KWS_HIT_WINDOW_S] + fresh
    sess.kws_peaks = [p for p in sess.kws_peaks if now - p[0] <= KWS_PEAK_WINDOW_S] + peaks
    if not fresh:
        return None
    # 音量门限看「这 800ms 窗内的峰值」，不是当前这一包：模型打分滞后，
    # 唤醒词说完那几包可能已经是静音了，按当包算会把真唤醒丢掉。
    win_peak = max([p for _, p in sess.kws_peaks], default=0)
    if win_peak < KWS_PEAK_MIN:
        log.info("sid=%s kws hit but quiet (peak=%d) -> 当误触", sess.sid, win_peak)
        return None
    for name in KWS_MODELS:
        hits = [h for h in sess.kws_hits if h[1] == name]
        if len(hits) >= KWS_NEED_FRAMES:   # 默认 800ms 内两帧过阈值
            if KWS_ASR_CONFIRM:
                ok, txt = _wake_asr_ok(sess)
                if not ok:
                    log.info("sid=%s KWS hit %s peak=%d frames=%d 但 ASR=%r 不像唤醒词 -> 当误触",
                             sess.sid, name, win_peak, len(hits), txt)
                    sess.wake_at = now          # 同一句话别反复触发
                    return None
                log.info("sid=%s KWS hit %s peak=%d frames=%d ASR=%r 确认",
                         sess.sid, name, win_peak, len(hits), txt)
            log.info("sid=%s KWS hit %s peak=%d frames=%d", sess.sid, name, win_peak, len(hits))
            return name
    return None


def vad_reset(sid):
    try:
        requests.post(VAD + "/vad_stream", params={"sid": sid, "reset": "1"}, data=b"",
                      timeout=PACKET_TIMEOUT)
    except Exception as e:
        log.warning("vad reset failed: %s", e)


def vad_feed(sid, pcm: bytes):
    r = requests.post(VAD + "/vad_stream", params={"sid": sid}, data=pcm, timeout=PACKET_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- 起一轮 / 悔棋 / 首句限长
def _strip_wake(s: str) -> str:
    """去掉唤醒词和标点，剩下的就是「这一轮真正说的话」。"""
    out = s or ""
    for w in WAKE_WORDS:
        out = out.replace(w, "")
    for ch in "，。！？、；：,.!?;:~ 　…—":
        out = out.replace(ch, "")
    return out


def _clean_user_text(s: str) -> str:
    """去掉唤醒词和首尾标点，得到真正送去大脑的那句话；只剩唤醒词时返回空串。"""
    out = s or ""
    for w in WAKE_WORDS:
        out = out.replace(w, "")
    return out.strip(" ，。！？、；：,.!?;:~…—\t\r\n\u3000")


# 收尾口令分两档（11-21 主人说「退下吧」，ASR 听成「退一下吧」→ 旧词表没命中，
# 而且「你先退一下吧，我这边在修一些小bug」超了 6 字限制，两处都漏）
END_STRONG = (           # 强口令：只要出现就收尾，不看句长（这些词日常几乎不会顺口说）
    "退下", "退一下", "退一", "退吧", "下去吧", "推下",
    "关掉", "关闭语音", "关闭对话", "关麦", "别再说话", "闭嘴",
    "别说了", "不聊了", "停下", "停止", "收工", "散了吧",
)
END_WEAK = (             # 弱口令：要求去掉唤醒词后 <= 6 字，免得「这个程序结束了没」误判
    "结束", "先这样", "再见", "睡觉了", "晚安", "好了",
)
END_WORDS = END_STRONG + END_WEAK


def _is_end_cmd(s: str) -> bool:
    """「退下 / 别说了 / 睡觉了」这类收尾口令：命中就不送大脑，直接把这一轮收掉。"""
    core = _strip_wake(s or "")
    if not core:
        return False
    if any(w in core for w in END_STRONG):
        return True
    if len(core) > 6:
        return False
    return any(w in core for w in END_WEAK)


def end_session(sess: Session, why: str):
    """把这一轮彻底收掉：停 TTS 队列、清 VAD、退回未唤醒。"""
    with _lock:
        sess.turn_seq += 1          # 跑着的 run_turn 发现编号变了就自己闭嘴
        sess.busy = False
        sess.pending = None
        sess.filler_q = None
        sess.prefix = ""
        sess.awake_at = 0.0
        sess.pre.clear()
        sess.events.clear()
        sess.n_seq = 0
    try:
        vad_reset(sess.sid)
    except Exception:
        pass
    log.info("sid=%s end session (%s)", sess.sid, why)


def _roll_add(sess, pcm: bytes):
    """滚动保留最近 2.5s 音频，KWS 命中时倒着取「命中前 2s」。"""
    if not pcm:
        return
    sess.roll += pcm
    cap = int((KWS_DUMP_PRE_S + 0.5) * 32000)
    if len(sess.roll) > cap:
        del sess.roll[:len(sess.roll) - cap]


def _dump_start(sess, model: str):
    """KWS 命中：开始攒 [命中前 2s, 命中后 1s]，攒满落 wav 到 /dl。"""
    if not KWS_DUMP_ON:
        return
    if sess.dump is not None:      # 上一段还没写完，先收掉
        _dump_write(sess)
    sess.dump = {"buf": bytearray(sess.roll), "need": int(KWS_DUMP_POST_S * 32000),
                 "model": model, "t": time.time()}
    log.info("sid=%s kws dump start (model=%s, pre=%.2fs)",
             sess.sid, model, len(sess.dump["buf"]) / 32000.0)


def _dump_add(sess, pcm: bytes):
    """命中之后继续喂进来的包，凑够「命中后 1s」就落盘。"""
    d = sess.dump
    if d is None:
        return
    d["buf"] += pcm
    d["need"] -= len(pcm)
    if d["need"] <= 0:
        _dump_write(sess)


def _dump_write(sess):
    d = sess.dump
    sess.dump = None
    if not d or not d["buf"]:
        return
    try:
        os.makedirs(KWS_DUMP_DIR, exist_ok=True)
        name = "kws_%s_%s_%s.wav" % (d["model"], time.strftime("%Y%m%d-%H%M%S"), sess.sid[-8:])
        path = os.path.join(KWS_DUMP_DIR, name)
        x = np.frombuffer(bytes(d["buf"]), dtype=np.int16)
        with open(path, "wb") as f:
            f.write(wav_bytes(x, 16000))
        log.info("sid=%s kws dump saved %s (%.2fs peak=%d)",
                 sess.sid, name, len(x) / 16000.0, int(np.abs(x).max()) if len(x) else 0)
    except Exception as e:
        log.warning("sid=%s kws dump failed: %s", sess.sid, e)


def _split_piece(s: str):
    """把一句长文本在第一处标点切成两截（首句别一上来就是长句）。"""
    head = s[:FIRST_PIECE_MAX]
    cut = -1
    for i, ch in enumerate(head):
        if ch in "，。！？；、,.!?;":
            cut = i
    if cut < 8:
        cut = min(len(s), FIRST_PIECE_MAX) - 1
    a = s[:cut + 1].strip()
    b = s[cut + 1:].strip()
    return [x for x in (a, b) if x]


def _cap_first(stream):
    """大脑给的第一句超过 FIRST_PIECE_MAX 字就切成两截，先播前半截（首句快出来）。"""
    first = True
    for p in stream:
        if first:
            first = False
            if len(p) > FIRST_PIECE_MAX:
                for q in _split_piece(p):
                    yield q
                continue
        yield p


def fire_turn(sess, text, want_filler, deliver_filler=False):
    """真开始这一轮：开线程问大脑 + 合成；返回垫音 payload（deliver_filler 时改成稍后随包下发）。"""
    sess.awake_at = time.time()
    with _lock:
        sess.busy = True
        sess.turn_seq += 1
        seq = sess.turn_seq
    threading.Thread(target=run_turn, args=(sess, text, bool(want_filler), seq), daemon=True).start()
    log.info("sid=%s turn start: %r", sess.sid, text[:40])
    if want_filler and FILLER:
        payload = {"event": "filler", "sid": sess.sid, "text": FILLER["text"],
                   "mime": FILLER["mime"], "audio_b64": FILLER["audio_b64"],
                   "more": "", "budget_ms": REPLY_BUDGET_MS}
        if deliver_filler:
            with _lock:
                sess.filler_q = payload
            log.info("sid=%s filler queued (deferred)", sess.sid)
            return None
        return payload
    return None


def schedule_pending(sess, pend):
    """end 之后先压 TURN_GRACE_MS 再回话：这期间又出声 -> 作废（悔棋）。"""
    def _run():
        time.sleep(TURN_GRACE_MS / 1000.0)
        with _lock:
            if sess.pending is not pend:
                return
            sess.pending = None
        fire_turn(sess, pend["text"], True, deliver_filler=True)
    threading.Thread(target=_run, daemon=True).start()


def schedule_after_undo(sess):
    """悔棋之后：如果主人其实已经说完了（一直没再出声），别干等 9884 的 6 秒超时。
    2 秒内没等到新的 end、也没有新的人声，就拿已经攒下的前缀开这一轮。
    任何新的人声都会把 resume_id +1，让这个定时器变成空操作。"""
    with _lock:
        sess.resume_id += 1
        rid = sess.resume_id
    threading.Timer(RESUME_AFTER_UNDO_S, _resume_fire, args=(sess, rid)).start()


def _resume_fire(sess, rid):
    with _lock:
        if rid != sess.resume_id:
            return                       # 期间又有人声/又判了一轮，放弃
        if sess.busy or sess.pending is not None:
            return
        text = _clean_user_text(sess.prefix)
        if len(text) < 2:
            return
        sess.prefix = ""
        sess.resume_id += 1
    log.info("sid=%s resume fire (悔棋后 %.1fs 无新声) text=%r", sess.sid, RESUME_AFTER_UNDO_S, text[:40])
    fire_turn(sess, text, True, deliver_filler=True)


# ---------------------------------------------------------------- 一轮回复
def run_turn(sess: Session, user_text: str, want_filler: bool, seq: int):
    """后台线程：问大脑 -> 逐句 TTS -> 推 reply/more；最后推 end。
    seq 是这一轮的编号：中途来了 /voice_edge/end 就把 sess.turn_seq +1，
    这里发现编号对不上就立刻闭嘴，别把已经作废的句子推给手机。"""
    sr = tts_sr()
    seg = int(sr * SEG_PCM_SEC)
    n_piece = 0
    total_samples = 0
    t_turn = time.time()
    try:
        for piece in _cap_first(brain_stream(user_text, sess.sid)):
            if sess.turn_seq != seq:
                log.info("sid=%s turn cancelled, stop before %r", sess.sid, piece[:16])
                return
            piece_t0 = time.time()
            part = [0]          # 这一句已经推出去几段（一句可能切成多段音频）

            def emit(pcm, _piece=piece, _part=part):
                nonlocal n_piece
                if sess.turn_seq != seq:
                    return
                first = (n_piece == 0)
                sess.push({"event": "reply" if first else "more",
                           "text": _piece if _part[0] == 0 else "",   # 文字只带在第一段
                           "part": _part[0], "mime": "audio/wav",
                           "audio_b64": b64_wav(pcm, sr), "sr": sr, "more": "",
                           "budget_ms": REPLY_BUDGET_MS})
                if first:
                    log.info("sid=%s reply first seg %.2fs after brain piece (%.2fs since turn start) ts=%dms",
                             sess.sid, time.time() - piece_t0, time.time() - t_turn,
                             int(time.time() * 1000))
                _part[0] += 1
                n_piece += 1

            n, _, _ = tts_stream(piece, emit, seg)
            total_samples += n
            if n == 0:
                log.warning("sid=%s tts 空输出 piece=%r", sess.sid, piece[:20])
        if n_piece == 0:
            log.warning("sid=%s 大脑没给任何可用文本（user=%r）", sess.sid, user_text[:30])
        sess.push({"event": "end", "text": "", "mime": "", "audio_b64": "", "more": ""})
        log.info("sid=%s turn done: pieces=%d audio=%.2fs wall=%.2fs",
                 sess.sid, n_piece, total_samples / float(sr or 1), time.time() - t_turn)
    except Exception as e:
        log.exception("sid=%s turn failed: %s", sess.sid, e)
        sess.push({"event": "end", "text": "", "mime": "", "audio_b64": "", "more": ""})
    finally:
        with _lock:
            sess.busy = False


# ---------------------------------------------------------------- 入口
@app.get("/health")
def health():
    """必须永远快：本地三件并行探（各 0.8s），大脑读后台线程的缓存，不在这里扫网段。"""
    from concurrent.futures import ThreadPoolExecutor

    def probe(url, path="/health"):
        try:
            return requests.get(url + path, timeout=0.8).status_code == 200
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=3) as ex:
        f1 = ex.submit(probe, VAD)
        f2 = ex.submit(probe, ASR)
        f3 = ex.submit(probe, TTS, "/docs")
        vad_ok, asr_ok, tts_ok = f1.result(), f2.result(), f3.result()
    brain_ok = bool(_brain["ok"])
    return {"ok": vad_ok and tts_ok, "vad": vad_ok, "asr": asr_ok, "tts": tts_ok,
            "brain": brain_ok, "brain_url": brain_url(),
            "filler": bool(FILLER), "tts_sr": _prompt_cache.get("sr"),
            "kws": KWS_ENABLED, "kws_models": list(KWS_MODELS),
            "seg_sec": SEG_PCM_SEC, "silent_peak": SILENT_PEAK,
            "fade_ms": FADE_MS, "lp": LP_CUTOFF,
            "grace_ms": TURN_GRACE_MS, "budget_ms": REPLY_BUDGET_MS,
            "first_max": FIRST_PIECE_MAX, "awake_keep_s": AWAKE_KEEP_S,
            "end": True, "end_words": list(END_WORDS),
            "kws_dump": KWS_DUMP_ON, "kws_dump_dir": KWS_DUMP_DIR,
            "sessions": len(_sessions)}


@app.post("/voice_edge/end")
def voice_edge_end(sid: str = ""):
    """主人说「退下」或手机主动收口：停 TTS 队列、清这个 sid 的 VAD、退回未唤醒。
    无 body，回 {"ok":true,"event":"end","sid":...}（11-21 隔壁提的接口）。"""
    sid = (sid or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "sid required"}, status_code=400)
    sess = _sessions.get(sid)
    if sess is None:
        return {"ok": True, "event": "end", "sid": sid, "note": "no such session"}
    end_session(sess, "api")
    return {"ok": True, "event": "end", "sid": sid}


@app.post("/voice_edge")
async def voice_edge(request: Request):
    t0 = time.time()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    sid = (body.get("sid") or "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "sid required"}, status_code=400)
    sess = get_session(sid)
    sess.n_pkt += 1

    # 播放期：麦克风不可信，整包丢掉；顺手把 9884 的缓冲清掉，免得播放声被当输入
    if body.get("playing"):
        if not sess.playing:
            sess.playing = True
            vad_reset(sid)
            log.info("sid=%s playing=true, audio dropped & vad reset", sid)
        return {"event": "idle", "sid": sid}

    if sess.playing:
        sess.playing = False
        vad_reset(sid)

    # 悔棋窗口里攒下的垫音：下一个包顺路带回去
    if sess.filler_q:
        with _lock:
            fq = sess.filler_q
            sess.filler_q = None
        log.info("sid=%s filler delivered (deferred) %.0fms ts=%dms", sid, (time.time() - t0) * 1000,
                 int(time.time() * 1000))
        return fq

    if body.get("force"):
        vad_reset(sid)
        sess.awake_at = time.time()
        sess.wake_at = time.time()
        sess.n_err = 0
        sess.prefix = ""
        sess.pre.clear()
        sess.pending = None
        sess.filler_q = None
        sess.clear_events()
        log.info("sid=%s wake (force)", sid)
        return {"event": "wake", "model": "force", "sid": sid}

    raw = body.get("pcm") or ""
    try:
        pcm = base64.b64decode(raw) if raw else b""
    except Exception:
        return JSONResponse({"ok": False, "error": "pcm not base64"}, status_code=400)

    _roll_add(sess, pcm)     # 滚动缓冲：KWS 命中时倒取「命中前 2s」
    _dump_add(sess, pcm)     # 正在留证的话，这一包凑「命中后 1s」

    # 没醒：只跑 KWS（不喂 VAD，免得把无数「不是叫它」的音频攒进去）
    if not sess.awake():
        sess.pre.append(pcm)
        hit = kws_feed(sess, pcm)
        if not hit:
            return {"event": "idle", "sid": sid}
        _dump_start(sess, hit)   # 留证：命中前 2s 已经在 roll 里了
        vad_reset(sid)          # 唤醒词那几包不进这一轮
        replayed = 0
        prefix = ""
        for p in list(sess.pre):    # 但唤醒前 2s 要补回去：KWS 打分滞后，否则一句的开头被吃掉
            if not p:
                continue
            try:
                r = vad_feed(sid, p)
                replayed += 1
                if r.get("end"):
                    # 补进去的这截自己收尾了（一般就是「雅雅」两个字）：留作前缀，
                    # 会话立刻重开，别让后面的活包落进 1.5s 丢弃窗口
                    prefix += (r.get("text") or "")
                    vad_reset(sid)
            except Exception:
                break
        sess.pre.clear()
        sess.prefix = prefix.strip()
        sess.awake_at = time.time()
        sess.wake_at = time.time()
        sess.n_err = 0
        sess.clear_events()
        if sess.kws is not None:
            try:
                sess.kws.reset()
            except Exception:
                pass
        sess.kws_buf = np.zeros(0, dtype=np.int16)
        sess.kws_hits = []
        log.info("sid=%s wake (kws=%s) %.0fms preroll=%d", sid, hit, (time.time() - t0) * 1000, replayed)
        return {"event": "wake", "model": hit, "preroll": replayed, "sid": sid}

    if sess.busy:
        # 上一轮还在生成，这一包的音频不要了，别把两轮混一起
        return {"event": "state", "state": "thinking", "sid": sid}

    try:
        res = vad_feed(sid, pcm)
    except Exception as e:
        sess.n_err += 1
        log.warning("sid=%s vad failed (%d): %s", sid, sess.n_err, e)
        if sess.n_err >= 3:
            return {"event": "end", "sid": sid}
        return {"event": "recording", "sid": sid}

    speech = bool(res.get("speech"))
    end = bool(res.get("end"))
    text = (res.get("text") or "").strip()
    if speech:
        sess.awake_at = time.time()      # 只要还在说话就一直醒着，不用每次都喊「雅雅」

    # 悔棋：刚判了 end、还在窗口里，又听到人声 -> 这一轮不算说完，那截文本并回前缀
    # （既看 VAD 的 speech，也看能量：9884 收尾后的几十毫秒里 VAD 可能还没重新开口）
    loud = _peak(np.frombuffer(pcm, dtype=np.int16)) >= KWS_PEAK_MIN
    undone = False
    if sess.pending is not None and (speech or loud) and not end:
        pend = sess.pending
        sess.pending = None
        if pend.get("text"):
            # 只并「像一句话」的片段：VAD 在词中间截断时会吐出 1 个字的残渣（实测出现过「在」），
            # 并进前缀就变成「…很多了，在」，送进大脑会把句子读歪。
            frag = pend["text"].strip()
            if len(_strip_wake(frag)) >= 2 and frag not in sess.prefix:
                sess.prefix = (sess.prefix + "，" + frag).strip("，")
        undone = True
        log.info("sid=%s undo end, keep listening (prefix=%r)", sid, sess.prefix[:30])

    if not end:
        ev = {"event": "recording", "sid": sid}
        if speech or loud:
            with _lock:
                sess.resume_id += 1      # 有人声了，作废悔棋后的兜底定时器
        if undone:
            # 注意顺序：先按「本次有人声」把编号推上去，再登记兜底定时器拿最新编号，
            # 否则同一个请求里紧接着的 resume_id += 1 会把它当场作废（实测踩过）。
            schedule_after_undo(sess)   # 主人其实说完了就别干等 9884 的 6s 超时
        if speech:
            ev["state"] = "listening"
        return ev

    # ---- 这一轮说完了
    if sess.prefix:
        # 唤醒时补喂那截（「雅雅」）或用悔棋拼回来的前半句，用逗号接上这一截
        text = (sess.prefix.rstrip(" ，。") + "，" + text).strip()
        sess.prefix = ""
    if not text:
        log.info("sid=%s turn end without text (reason=%s)", sid, res.get("reason"))
        return {"event": "end", "sid": sid}

    if len(_strip_wake(text)) <= 1:
        # 只有「雅雅」两个字（外加 ASR 在尾巴上瞎补的一个字）自己收尾成一轮。这不能当一句话：
        # 去问大脑只会拿到空回复，然后回垫音 -> 手机进播放期闭麦 -> 主人后面那句被掐掉
        # （11-18 00:25 就是这么被打断的）
        log.info("sid=%s 只听到唤醒词 %r -> 继续听", sid, text[:20])
        sess.awake_at = time.time()
        vad_reset(sid)
        return {"event": "recording", "state": "listening", "sid": sid}

    text = _clean_user_text(text) or text   # 「雅雅」不算内容，别让它进大脑

    if _is_end_cmd(text):
        # 主人说「退下 / 睡觉了」：这一轮到此为止，不送大脑、不出声（11-21 隔壁提的需求）
        log.info("sid=%s end cmd %r -> 收掉这一轮，不送大脑", sid, text[:20])
        end_session(sess, "end_cmd")
        return {"event": "end", "sid": sid, "text": text, "reason": "end_cmd"}

    want_filler = bool(body.get("filler", True))
    if want_filler:
        pend = {"text": text, "t": time.time()}
        sess.pending = pend
        schedule_pending(sess, pend)
        log.info("sid=%s end: text=%r -> grace %dms ts=%dms", sid, text[:30], TURN_GRACE_MS,
                 int(time.time() * 1000))
        return {"event": "recording", "state": "listening", "sid": sid}

    ev = fire_turn(sess, text, False)
    if ev:
        return ev

    # 没开垫音：短等一会儿，把第一段 reply 直接带在响应里
    deadline = time.time() + REPLY_WAIT_MS / 1000.0
    while time.time() < deadline:
        ev2 = sess.pop()
        if ev2:
            if ev2.get("event") == "reply":
                return ev2
            sess.push(ev2)   # 不是 reply（比如已经 end）就放回去，交给轮询
            break
        time.sleep(0.05)
    return {"event": "state", "state": "thinking", "sid": sid}


@app.post("/api/voice/reply")
async def api_voice_reply(request: Request):
    """手机端原生路径。body {"code": 口令, "token": "<sid>:<序号>"}。
    回 {"ok":true,"payload":{一段 reply/more/end}} / {"waiting":true} / {"lost":true}。
    payload 里带 i（本段序号）和 next（下一轮该用的序号）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
    token = str(body.get("token") or "")
    sid, _, seq_s = token.partition(":")
    if not sid:
        return {"ok": True, "lost": True}
    try:
        seq = int(seq_s) if seq_s else 0
    except ValueError:
        return {"ok": True, "lost": True}
    sess = get_session(sid)
    with _lock:
        if not sess.events:
            return {"ok": True, "waiting": True}
        head = sess.events[0]
        hi = head.get("i", 0)
        if seq < hi:                      # 客户端漏了（或旧 token），从当前段重新给
            return {"ok": True, "lost": True, "next": hi}
        if seq > hi:                      # 还没轮到，等一会儿再来
            return {"ok": True, "waiting": True, "next": hi}
        ev = sess.events.popleft()
    return {"ok": True, "payload": ev}


@app.get("/voice_edge/reply")
def voice_edge_reply(sid: str = "", wait: int = 0):
    """客户端轮询取下一段。wait 是长轮询毫秒数（<=5000）。"""
    if not sid:
        return JSONResponse({"ok": False, "error": "sid required"}, status_code=400)
    sess = get_session(sid)
    deadline = time.time() + min(max(wait, 0), 5000) / 1000.0
    while True:
        ev = sess.pop()
        if ev:
            return ev
        if time.time() >= deadline:
            return {"event": "idle", "sid": sid}
        time.sleep(0.03)


@app.get("/voice_edge/turns")
def turns():
    now = time.time()
    return {"ok": True, "n": len(_sessions),
            "sessions": [{"sid": s.sid, "awake": s.awake(), "busy": s.busy,
                          "queued": len(s.events), "idle": round(now - s.last, 1),
                          "pkts": s.n_pkt} for s in _sessions.values()]}


@app.get("/tts")
async def tts_get(request: Request):
    """给 AstrBot（QQ 侧）用的极简口子：只要 text，其余参数这里配好了。
    多余的 query 参数（text_lang / media_type / ref_audio_path …）全部忽略。"""
    q = dict(request.query_params)
    text = (q.get("text") or q.get("msg") or "").strip()[:600]
    if not text:
        return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
    fmt = (q.get("media_type") or "wav").lower()
    if fmt not in ("wav", "mp3", "ogg", "aac", "raw"):
        fmt = "wav"
    try:
        data = await asyncio.to_thread(tts_wav, text, q.get("speed_factor"), fmt, q)
    except Exception as e:
        log.warning("tts_get failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg",
            "aac": "audio/aac", "raw": "audio/L16"}[fmt]
    return Response(content=data, media_type=mime)


@app.post("/tts")
async def tts_post(request: Request):
    """同上，POST 版本；body 可以是 JSON {"text": "..."} 或表单。"""
    text, speed, extra = "", None, {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            text = str(body.get("text") or body.get("msg") or "")
            speed = body.get("speed_factor")
            extra = body
    except Exception:
        pass
    if not text:
        try:
            form = await request.form()
            text = str(form.get("text") or "")
            speed = form.get("speed_factor")
            extra = dict(form)
        except Exception:
            pass
    text = text.strip()[:600]
    if not text:
        return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
    try:
        data = await asyncio.to_thread(tts_wav, text, speed, "wav", extra)
    except Exception as e:
        log.warning("tts_post failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    return Response(content=data, media_type="audio/wav")


def filler_retry():
    """开机时 TTS 可能还没就绪，垫音会生成失败；这里一直重试到成功为止。"""
    global FILLER
    while True:
        if FILLER:
            return
        time.sleep(20)
        if FILLER:
            return
        try:
            if not tts_sr():
                continue
            FILLER = make_filler()
        except Exception as e:
            log.warning("filler retry 失败：%s", e)


def reaper():
    while True:
        time.sleep(10)
        now = time.time()
        # 回完话之后一直没包：退回 idle，别让手机以为还在 thinking
        for s in list(_sessions.values()):
            # 留证只攒到一半、后面没包了（主人喊完就安静）：3 秒后按现有的落盘，别烂在内存里
            if s.dump is not None and now - s.dump["t"] > 3.0:
                _dump_write(s)
            if not s.busy and s.pending is None and s.awake() and now - s.last > IDLE_RESET_S:
                s.awake_at = 0.0
                s.pre.clear()
                log.info("sid=%s idle %.0fs after turn -> back to idle", s.sid, now - s.last)
        with _lock:
            dead = [k for k, s in _sessions.items() if now - s.last > SESSION_IDLE_S and not s.busy]
            for k in dead:
                _sessions.pop(k, None)
        for k in dead:
            log.info("sid=%s reaped", k)


FILLER = {}

if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.path.join(HERE, "voice_edge.out"), "a", encoding="utf-8")
    log.info("voice_edge starting: vad=%s tts=%s brain=%s", VAD, TTS, BRAIN)
    try:
        tts_sr()
        FILLER = make_filler()
    except Exception as e:
        log.warning("warmup 失败：%s", e)
    if KWS_ENABLED:
        try:
            t_k = time.time()
            new_kws_model()
            log.info("kws warmup %.0fms models=%s th=%s frames>=%d", (time.time() - t_k) * 1000,
                     list(KWS_MODELS), {k: v[1] for k, v in KWS_MODELS.items()}, KWS_NEED_FRAMES)
        except Exception as e:
            log.warning("kws warmup 失败：%s", e)
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=filler_retry, daemon=True).start()
    threading.Thread(target=brain_watch, daemon=True).start()
    uvicorn.run(app, host=os.environ.get("VOICE_HOST", "0.0.0.0"),
            port=int(os.environ.get("VOICE_PORT", "8780")), log_config=None, access_log=False)

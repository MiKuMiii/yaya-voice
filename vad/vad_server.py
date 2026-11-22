# -*- coding: utf-8 -*-
"""语音端点(VAD)服务 — 给隔壁用的「wav/PCM -> 语音段区间 [+ 文本]」端点

跑在 9884，CPU 上跑 fsmn-vad，不占 9883 的显存，也不影响正在用的 paraformer 服务。
转写走 9883 已有的 /asr（同一张卡，仍然只有一个进程在推理）。

端点：
  GET  /health                      -> {"ok":true,"model":"fsmn-vad","device":"cpu"}
  POST /vad?sr=16000                请求体=裸 PCM16LE -> {"ok":true,"segments":[[s,e],...],"ms":n}
  POST /vad_file                    请求体=WAV 文件     -> {"ok":true,"segments":[[s,e],...],"dur":x,"texts":[...],"text":"..."}
      参数 asr=0 只要区间不要文本；参数 max_seg_ms=15000 超长段会被切开
  POST /vad_stream?sid=xxx          请求体=裸 PCM16LE（每包 0.5 秒）
       -> {"sid","state":"idle|listening|thinking","speech","end","seg","text"}
       ?reset=1 强制重开一轮；GET /vad_stream/sessions 看活着的会话

时间单位一律毫秒。
"""
import asyncio
import io
import logging
import logging.handlers
import os
import sys
import threading
import time
import wave

import numpy as np
import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
P_VAD = os.path.join(MODELS, "speech_fsmn_vad_zh-cn-16k-common-pytorch")
ASR_URL = os.environ.get("VOICE_ASR", "http://127.0.0.1:9883") + "/asr"
PORT = int(os.environ.get("VAD_PORT", "9884"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.handlers.RotatingFileHandler(
                  os.path.join(HERE, "vad.log"), maxBytes=20 * 1024 * 1024,
                  backupCount=3, encoding="utf-8")],
)
log = logging.getLogger("vad")

app = FastAPI()
_vad = None
_vad_stream = None


def get_vad():
    global _vad
    if _vad is None:
        from funasr import AutoModel
        t0 = time.time()
        _vad = AutoModel(model="fsmn-vad", model_path=P_VAD, device="cpu", disable_update=True)
        log.info("vad model loaded in %d ms", (time.time() - t0) * 1000)
    return _vad


def get_vad_stream():
    """流式那路单独一个实例：DynamicStreamingVAD 会改 model.vad_opts，别污染 /vad 的默认参数。"""
    global _vad_stream
    if _vad_stream is None:
        from funasr import AutoModel
        t0 = time.time()
        _vad_stream = AutoModel(model="fsmn-vad", model_path=P_VAD, device="cpu",
                                disable_update=True, disable_pbar=True)
        log.info("vad stream model loaded in %d ms", (time.time() - t0) * 1000)
    return _vad_stream


def pcm_from_bytes(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def pcm_from_wav(raw: bytes):
    with wave.open(io.BytesIO(raw), "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        frames = w.readframes(n)
    if sw != 2:
        raise ValueError("expect 16-bit wav, got %d-bit" % (sw * 8))
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        # 线性插值重采样，够用
        idx = np.linspace(0, pcm.size - 1, int(pcm.size * 16000 / sr))
        pcm = np.interp(idx, np.arange(pcm.size), pcm).astype(np.float32)
    return pcm


def run_vad(pcm: np.ndarray, max_seg_ms: int = 0):
    res = get_vad().generate(input=pcm, fs=16000)
    segs = []
    if res:
        r = res[0]
        segs = r.get("value") or r.get("text") or []
    out = []
    for s in segs:
        try:
            a, b = int(s[0]), int(s[1])
        except Exception:
            continue
        if max_seg_ms and b - a > max_seg_ms:
            t = a
            while t < b:
                out.append([t, min(t + max_seg_ms, b)])
                t += max_seg_ms
        else:
            out.append([a, b])
    return out


def asr_pcm(pcm: np.ndarray):
    body = (np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes()
    try:
        r = requests.post(ASR_URL, params={"sr": 16000}, data=body, timeout=60)
        j = r.json()
        return (j.get("text") or "").strip()
    except Exception as e:
        log.warning("asr call failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# /vad_stream —— 每 0.5 秒一包的在线端点，给手机端做「唯一状态权威」
#
# 客户端 POST ?sid=xxx，body=裸 PCM16LE 8000 样本（0.5 秒）。
# 返回 {"sid","state","speech","end","seg","text"}：
#   state: idle（本轮还没人声）| listening（正在听/等你说完）| thinking（刚判完，去转写回话）
#   speech: 这 0.5 秒里有没有人声（帧级能量 + 自适应底噪，只用于 UI 提示）
#   end:    这一轮该收尾了（静音够了 / 超时 / 到长度上限）
#   seg:    本轮已确认的人声区间（毫秒，相对本轮开头）
#   text:   仅在 end=true 时给，直接用下面的 paraformer 转写，省客户端再调 9883
# ---------------------------------------------------------------------------

# 累积语音时长上限 -> 判「说完了」需要的静音时长。短句多等一点（别切碎），长句快切。
STREAM_SCHEDULE = [(4000, 700), (8000, 600), (15000, 500), (30000, 450), (float("inf"), 400)]
STREAM_MIN_SEG_MS = 350        # 短于这个的语音段当噪声（播放尾巴），不收尾
STREAM_QUIET_TIMEOUT_S = 6.0   # 本轮开了但一直没人声 -> 收尾，别让客户端卡在 listening
STREAM_QUIET_CLOSE_MS = 1800   # VAD 卡在 speaking、但音频已经静了这么久 -> 强制收尾（别把整轮丢掉）
# 11-18 隔壁真机发现 1200ms 太短：中文口语句间停顿常到 1.2~1.5s，会把一句切成两轮 -> 手机以为说完了
STREAM_MAX_SEG_MS = 12000      # 单个语音段超过这个长度还没收尾 -> 强制收尾（VAD 被噪声黏住时的兜底）
STREAM_MAX_TURN_S = 30.0       # 单轮上限
STREAM_IDLE_REAP_S = 60.0      # 60 秒没包就回收
STREAM_REOPEN_GAP_S = 0.35      # 上一轮判完后，包间隔超过这个值就当新的一轮重开
                               # （客户端 0.5s 一包，1.5s = 连丢两包，留够抖动余量；
                               #   而播放期停发必然远超 1.5s，不会误判）
STREAM_CLOSED_MAX_S = 20.0     # 判完后客户端一直不停发（>这么多秒没断），也强制重开
STREAM_MAX_BUF_S = 40.0        # 缓冲上限（防御性）

_cls_dyn = None
_sessions = {}
_lock = threading.Lock()
_last_sweep = [0.0]


def dyn_cls():
    global _cls_dyn
    if _cls_dyn is None:
        from funasr.models.fsmn_vad_streaming.dynamic_vad import DynamicStreamingVAD
        _cls_dyn = DynamicStreamingVAD
    return _cls_dyn


class StreamSession:
    def __init__(self, sid: str):
        self.sid = sid
        self.vad = dyn_cls()(get_vad_stream(), chunk_size_ms=60, speech_noise_thres=0.5,
                             silence_schedule=STREAM_SCHEDULE)
        self.buf = []
        self.n = 0                     # 本轮样本数
        self.floor = 0.003             # 自适应底噪
        self.segs = []                 # 本轮确认的人声段 [[s,e],...] ms
        self.spoke = False             # 本轮出现过人声（哪怕 VAD 还没给出结束信号）
        self.quiet_ms = 0              # 连续静音帧累计（能量口径，用来兜住 VAD 不闭段的情况）
        self.t0 = time.time()
        self.last = time.time()
        self.tickets = 0
        self.closed = False            # True = 本轮已判完，等在途包丢掉
        self.closed_at = 0.0

    # --- 本轮音频（给 ASR 用） ---
    def audio(self, a_ms: int, b_ms: int):
        a = max(0, int(a_ms * 16)); b = min(self.n, int(b_ms * 16))
        if b <= a:
            return None
        return np.concatenate(self.buf)[a:b]

    def reset_turn(self):
        self.buf = []
        self.n = 0
        self.segs = []
        self.spoke = False
        self.quiet_ms = 0
        self.t0 = time.time()
        self.closed = True          # 本轮判完了，等在途/播放期的包别把状态扳回 listening
        self.closed_at = time.time()
        self.vad.reset()

    def reopen(self):
        self.buf = []
        self.n = 0
        self.segs = []
        self.spoke = False
        self.quiet_ms = 0
        self.closed = False
        self.t0 = time.time()
        self.vad.reset()

    def feed(self, pcm: np.ndarray):
        """吃 0.5 秒，返回 (speech, end, reason, text)"""
        if pcm.size:
            self.buf.append(pcm)
            self.n += pcm.size
            if len(self.buf) > 8 and self.n > STREAM_MAX_BUF_S * 16000:
                self.buf = [np.concatenate(self.buf)[-int(STREAM_MAX_BUF_S * 16000):]]
                self.n = self.buf[0].size
        self.last = time.time()
        self.tickets += 1

        import torch
        new = self.vad.feed(torch.from_numpy(pcm.copy()).float(), is_final=False) or []
        # speech 以 VAD 为准：正在段内（is_speaking）或这一包刚触发端点信号。
        # 不用裸能量判，否则播放尾巴那种瞬态又会被当成"人声"。
        speech = bool(self.vad.is_speaking or new)
        if speech:
            self.spoke = True
        # 静音计数只看能量，不看 VAD 的 is_speaking：VAD 要是卡在 speaking 不出来，
        # 靠 VAD 自己永远等不到收尾。
        if pcm.size:
            span = self._quiet_span_ms(pcm)
            pkt_ms = pcm.size * 1000 // 16000
            self.quiet_ms = self.quiet_ms + span if span >= pkt_ms else span
        end_ms = None
        for s, e in new:
            if e < 0 or s < 0:
                continue
            if e - s >= STREAM_MIN_SEG_MS:
                self.segs.append([int(s), int(e)])
                end_ms = int(e)
            else:
                log.info("sid=%s ignore short seg %s (%dms)", self.sid, [s, e], e - s)

        # VAD 还挂在 speaking、可这一包的尾巴已经静够了：别等客户端补静音包，
        # 就地强制收尾。否则会出现"VAD 一直不闭段 -> 整轮被超时丢掉"。
        if end_ms is None and self.vad.is_speaking and self.quiet_ms >= STREAM_QUIET_CLOSE_MS:
            forced = self._force_close()
            if forced is not None:
                end_ms = forced
                log.info("sid=%s force close quiet=%dms seg=%s",
                         self.sid, self.quiet_ms, self.segs[-1])

        elapsed = self.n / 16000.0
        wall = time.time() - self.t0
        end = False
        reason = ""
        if end_ms is not None:
            end, reason = True, "silence"
        elif not self.spoke and (elapsed >= STREAM_QUIET_TIMEOUT_S or wall >= STREAM_QUIET_TIMEOUT_S):
            # 整个回合一个人声都没有 -> 这才叫超时（以前只要"没闭合的段"就判超时，
            # 语音一直连到缓冲末尾时会把真人声当成没说话，整轮丢掉）
            end, reason = True, "timeout"
        elif self.vad.is_speaking and int(self.vad.current_duration_ms) >= STREAM_MAX_SEG_MS:
            # 一段"说话"超过 12 秒还不收尾：多半是噪声把 VAD 黏住了，强制收尾，
            # 别让客户端一直卡在 listening。
            end, reason = True, "maxlen"
            self._force_close()
        elif elapsed >= STREAM_MAX_TURN_S:
            end, reason = True, "maxlen"
            self._force_close()

        text = ""
        if end and self.segs:
            a = self.segs[0][0] - 150
            b = self.segs[-1][1] + 250
            piece = self.audio(a, b)
            if piece is not None:
                text = asr_pcm(piece)
            log.info("sid=%s turn end reason=%s dur=%.2fs segs=%s text=%r",
                     self.sid, reason, elapsed, self.segs, text[:40])
        elif end:
            log.info("sid=%s turn end reason=%s dur=%.2fs (no speech)", self.sid, reason, elapsed)
        return speech, end, reason, text

    def _quiet_span_ms(self, pcm: np.ndarray) -> int:
        """这一包末尾连续静音有多长（20ms 一帧，能量口径）。"""
        if pcm.size < 320:
            return 0
        fr = pcm[: pcm.size // 320 * 320].reshape(-1, 320)  # 20ms
        rms = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
        thr = max(self.floor * 2.0, 0.004)
        span = 0
        for r in rms[::-1]:
            if r > thr:
                break
            span += 1
        for r in rms:                                  # 底噪只跟安静的帧走，别被语音抬上去
            if r < self.floor:
                self.floor = 0.9 * self.floor + 0.1 * r
            else:
                self.floor = 0.998 * self.floor + 0.002 * r
        return span * 20

    def _force_close(self):
        """VAD 不给结束信号时，把还开着的段收掉；返回段尾 ms，收不出来返回 None。"""
        n_ms = self.n * 1000 // 16000
        got = None
        try:
            new = self.vad.finalize() or []
        except Exception as e:
            log.warning("sid=%s finalize failed: %s", self.sid, e)
            new = []
        for s, e in new:
            if s < 0 or e < 0:
                continue
            if e - s >= STREAM_MIN_SEG_MS:
                self.segs.append([int(s), int(e)])
                got = int(e)
        if got is None and self.vad.is_speaking:
            start = max(0, n_ms - int(self.vad.current_duration_ms))
            if n_ms - start >= STREAM_MIN_SEG_MS:
                self.segs.append([start, n_ms])
                got = n_ms
                log.info("sid=%s synthesize tail seg %s", self.sid, self.segs[-1])
        return got

    def _energy_speech(self, pcm: np.ndarray) -> bool:
        """备用：裸能量判人声（现在不用，VAD 说了算）。"""
        if pcm.size < 320:
            return False
        fr = pcm[: pcm.size // 320 * 320].reshape(-1, 320)  # 20ms
        rms = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
        thr = max(self.floor * 2.5, 0.004)
        voiced = bool(np.any(rms > thr))
        for r in rms:
            if r < self.floor:
                self.floor = 0.9 * self.floor + 0.1 * r
            else:
                self.floor = 0.998 * self.floor + 0.002 * r
        return voiced


def sweep():
    now = time.time()
    if now - _last_sweep[0] < 10.0:
        return
    _last_sweep[0] = now
    drop = [k for k, s in _sessions.items() if now - s.last > STREAM_IDLE_REAP_S]
    for k in drop:
        _sessions.pop(k, None)
    if drop:
        log.info("reaped sessions %s (left %d)", drop, len(_sessions))


def stream_process(sid: str, raw: bytes, reset: bool):
    with _lock:
        if reset:
            _sessions.pop(sid, None)
        s = _sessions.get(sid)
        pcm = pcm_from_bytes(raw) if raw else np.zeros(0, dtype=np.float32)
        now = time.time()
        if s is None:
            s = _sessions[sid] = StreamSession(sid)
            log.info("sid=%s new session (live %d)", sid, len(_sessions))
        elif s.closed:
            # 上一轮已判完：播放期/在想的时候客户端可能还在发包。
            # 判完后 0.35s 内的包算上一轮尾巴，回 thinking 丢掉（别把状态扳回 listening）；
            # 过了这个窗口就当新的一轮重开。
            # 注意：门槛用「距判完的时间」而不是「距上一个包的间隔」——
            # 客户端补发/burst 时包间隔可能只有几十毫秒，用间隔判会永远卡在 closed 里。
            if now - s.closed_at < STREAM_REOPEN_GAP_S and now - s.closed_at < STREAM_CLOSED_MAX_S:
                s.last = now
                s.tickets += 1
                return {"sid": sid, "state": "thinking", "speech": False, "end": False,
                        "seg": [], "ms": 1, "dropped": True}
            log.info("sid=%s reopened (closed=%.1fs)", sid, now - s.closed_at)
            s.reopen()

        t0 = time.time()
        speech, end, reason, text = s.feed(pcm)
        segs = [list(x) for x in s.segs]
        dur = round(s.n / 16000.0, 2)
        if end:
            state = "thinking"
            speech = False          # 已经判完，别再让客户端显示"在听"
            s.reset_turn()
        elif s.segs or speech:
            state = "listening"
        else:
            state = "listening" if s.tickets > 1 else "idle"
        out = {"sid": sid, "state": state, "speech": speech, "end": end,
               "seg": segs, "ms": int((time.time() - t0) * 1000)}
        if end:
            out["reason"] = reason
            out["turn_ms"] = int(dur * 1000)
            if text:
                out["text"] = text
        sweep()
        return out


@app.get("/health")
def health():
    return {"ok": True, "model": "fsmn-vad", "device": "cpu", "asr_proxy": ASR_URL,
            "stream": True, "live_sessions": len(_sessions)}


@app.post("/vad_stream")
async def vad_stream(request: Request):
    sid = request.query_params.get("sid", "").strip()
    if not sid:
        return JSONResponse({"ok": False, "error": "sid required"}, status_code=400)
    reset = request.query_params.get("reset", "0") == "1"
    raw = await request.body()
    if len(raw) < 320:
        # reset=1 且不带音频：只做「武装新一轮」，返回 idle，方便客户端唤醒时先踢一脚
        if reset:
            with _lock:
                _sessions.pop(sid, None)
            return {"sid": sid, "state": "idle", "speech": False, "end": False, "seg": [], "ms": 0}
        return JSONResponse({"ok": False, "error": "body too short (%d bytes)" % len(raw)},
                            status_code=400)
    try:
        return await asyncio.to_thread(stream_process, sid, raw, reset)
    except Exception as e:
        log.exception("vad_stream failed sid=%s", sid)
        return JSONResponse({"ok": False, "error": str(e), "sid": sid}, status_code=500)


@app.get("/vad_stream/sessions")
def vad_sessions():
    now = time.time()
    return {"ok": True, "n": len(_sessions),
            "sessions": [{"sid": s.sid, "dur": round(s.n / 16000.0, 2),
                          "idle": round(now - s.last, 1), "segs": s.segs,
                          "tickets": s.tickets} for s in _sessions.values()]}


@app.post("/vad")
async def vad(request: Request):
    t0 = time.time()
    raw = await request.body()
    if not raw:
        return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
    try:
        pcm = pcm_from_bytes(raw)
        segs = run_vad(pcm, int(request.query_params.get("max_seg_ms", "0")))
        return {"ok": True, "segments": segs, "dur": round(pcm.size / 16000.0, 2),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        log.exception("vad failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/vad_file")
async def vad_file(request: Request):
    t0 = time.time()
    raw = await request.body()
    if not raw:
        return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)
    want_asr = request.query_params.get("asr", "1") != "0"
    max_seg = int(request.query_params.get("max_seg_ms", "15000"))
    try:
        pcm = pcm_from_wav(raw)
        segs = run_vad(pcm, max_seg)
        out = {"ok": True, "dur": round(pcm.size / 16000.0, 2), "segments": segs,
               "ms": int((time.time() - t0) * 1000)}
        if want_asr:
            texts, pieces = [], []
            for a, b in segs:
                piece = pcm[int(a * 16):int(b * 16)]
                t = asr_pcm(piece)
                texts.append(t)
                if t:
                    pieces.append(t)
            out["texts"] = texts
            out["text"] = "".join(pieces)
        return out
    except Exception as e:
        log.exception("vad_file failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    # pythonw 下 sys.stdout / sys.stderr 都是 None：
    # uvicorn 的日志 formatter 会调用 sys.stdout.isatty() 直接崩，所以两个都要接管
    if sys.stdout is None:
        sys.stdout = open(os.path.join(HERE, "vad.out"), "a", buffering=1, encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.path.join(HERE, "vad.err"), "a", buffering=1, encoding="utf-8")
    log.info("starting vad server on :%d", PORT)
    try:
        get_vad()
        get_vad_stream()
    except Exception:
        log.exception("warmup failed (ignored)")
    # log_config=None: 不装 uvicorn 自带的 logging 配置，避免它去碰 sys.stdout
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning",
                log_config=None, access_log=False)

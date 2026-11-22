package com.example.voicelink;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.IBinder;
import android.util.Base64;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.concurrent.ConcurrentLinkedQueue;

/**
 * 这条链路里最薄的一层：录音 -> 每 0.5 秒发一包 -> 收事件 -> 放音频。
 * 判唤醒、转写、合成都在边缘侧，这里不猜。
 */
public class VoiceService extends Service {
    static final String TAG = "VoiceLink";
    public static final String ACTION_START = "voicelink.START";
    public static final String ACTION_STOP = "voicelink.STOP";

    public static volatile String STATUS = "待命";
    public static volatile String LAST_TEXT = "";

    static final int SR = 16000;
    static final int BLOCK = 8000;          // 0.5 秒
    static final int WAKE_PEAK = 600;       // 非唤醒模式下，太安静就不发，省流量
    static final String CHANNEL = "voicelink";

    volatile boolean running = false;
    volatile boolean woke = false;
    volatile boolean playing = false;
    String base, code, sid;

    AudioRecord rec;
    AudioTrack track;
    int trackRate = 0;

    final ConcurrentLinkedQueue<byte[]> queue = new ConcurrentLinkedQueue<>();
    Thread playLoop;
    volatile boolean playThreadOn = false;

    @Override
    public IBinder onBind(Intent i) {
        return null;
    }

    @Override
    public int onStartCommand(Intent it, int flags, int startId) {
        String act = it == null ? null : it.getAction();
        if (ACTION_STOP.equals(act)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (running) return START_STICKY;

        base = Config.url(this).replaceAll("/+$", "");
        code = Config.code(this);
        sid = "phone-" + Long.toHexString(System.currentTimeMillis());
        startForegroundSafe();
        running = true;
        STATUS = "连接中……";

        Thread cap = new Thread(this::capture, "cap");
        cap.start();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        woke = false;
        try {
            if (base != null) sendEnd();
        } catch (Exception ignore) {
        }
        try {
            if (rec != null) {
                rec.stop();
                rec.release();
                rec = null;
            }
        } catch (Exception ignore) {
        }
        stopPlay();
        STATUS = "待命";
        super.onDestroy();
    }

    void startForegroundSafe() {
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                nm.createNotificationChannel(
                        new NotificationChannel(CHANNEL, "VoiceLink", NotificationManager.IMPORTANCE_LOW));
            }
            Notification n = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(this, CHANNEL).setContentTitle("VoiceLink").setContentText("正在通话").setSmallIcon(android.R.drawable.ic_btn_speak_now).build()
                    : new Notification.Builder(this).setContentTitle("VoiceLink").setContentText("正在通话").build();
            startForeground(1, n);
        } catch (Exception e) {
            Log.w(TAG, "startForeground 失败", e);
        }
    }

    // ---------- 采集 ----------

    void capture() {
        int min = AudioRecord.getMinBufferSize(SR, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, SR,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, BLOCK * 2));
        rec.startRecording();
        byte[] buf = new byte[BLOCK];
        while (running) {
            int n = rec.read(buf, 0, BLOCK);
            if (n <= 0) continue;
            byte[] pcm = new byte[n];
            System.arraycopy(buf, 0, pcm, 0, n);

            if (playing) continue;                    // 放音期间不外发，免回灌
            if (!woke && peak(pcm) < WAKE_PEAK) continue;   // 冷场就不发

            try {
                JSONObject body = new JSONObject();
                body.put("code", code);
                body.put("sid", sid);
                body.put("pcm", Base64.encodeToString(pcm, Base64.NO_WRAP));
                body.put("force", false);
                body.put("filler", true);
                body.put("playing", playing);
                handle(post(base + "/voice_edge", body.toString(), 8000));
            } catch (Exception e) {
                Log.w(TAG, "上行失败", e);
                sleep(400);
            }
        }
    }

    int peak(byte[] pcm) {
        int mx = 0;
        for (int i = 0; i + 1 < pcm.length; i += 2) {
            int v = (short) ((pcm[i] & 0xff) | (pcm[i + 1] << 8));
            if (v < 0) v = -v;
            if (v > mx) mx = v;
        }
        return mx;
    }

    // ---------- 事件 ----------

    void handle(String json) {
        if (json == null || json.isEmpty()) return;
        try {
            JSONObject o = new JSONObject(json);
            String ev = o.optString("event", "");
            if ("wake".equals(ev)) {
                woke = true;
                STATUS = "在听";
            } else if ("recording".equals(ev)) {
                woke = true;
                STATUS = "在听，说完停一下";
            } else if ("filler".equals(ev)) {
                playB64(o.optString("audio_b64"), o.optString("mime"));
                STATUS = "在想……";
                startPoll(o.optString("token"));
            } else if ("reply".equals(ev) || "more".equals(ev)) {
                String text = o.optString("text", "");
                if (!text.isEmpty()) LAST_TEXT = text;
                STATUS = "在说";
                playB64(o.optString("audio_b64"), o.optString("mime"));
                String more = o.optString("more", "");
                if (!more.isEmpty()) startPoll(more);
            } else if ("end".equals(ev)) {
                woke = false;
                STATUS = "待命";
            } else if ("idle".equals(ev)) {
                // 没说话 / 被打断，保持原状
            } else if ("stop".equals(ev)) {
                stopPlay();
                woke = true;
                STATUS = "不说了，你说";
            } else if ("error".equals(ev)) {
                STATUS = "出错：" + o.optString("error");
            }
        } catch (Exception e) {
            Log.w(TAG, "解析失败: " + json, e);
        }
    }

    volatile boolean pollLed = false;

    void startPoll(final String token) {
        if (pollLed) return;
        pollLed = true;
        new Thread(() -> {
            try {
                long t0 = System.currentTimeMillis();
                while (running && System.currentTimeMillis() - t0 < 60000) {
                    String r = get(base + "/voice_edge/reply?sid=" + url(sid) + "&wait=3000", 12000);
                    if (r == null || r.isEmpty()) continue;
                    JSONObject o = new JSONObject(r);
                    String ev = o.optString("event", "");
                    if ("idle".equals(ev) && System.currentTimeMillis() - t0 < 1800) {
                        sleep(250);
                        continue;
                    }
                    handle(r);
                    if ("end".equals(ev) || "idle".equals(ev)) break;
                }
            } catch (Exception e) {
                Log.w(TAG, "轮询失败", e);
            } finally {
                pollLed = false;
            }
        }, "poll").start();
    }

    // ---------- 播放 ----------

    void playB64(String b64, String mime) {
        if (b64 == null || b64.isEmpty()) return;
        byte[] data = Base64.decode(b64, Base64.DEFAULT);
        if (data.length < 44) return;
        queue.add(data);
        if (!playThreadOn) {
            playThreadOn = true;
            playLoop = new Thread(this::playLoop, "play");
            playLoop.start();
        }
    }

    void playLoop() {
        playing = true;
        try {
            while (running) {
                byte[] data = queue.poll();
                if (data == null) break;
                int[] info = wavInfo(data);
                if (info == null) continue;
                ensureTrack(info[0], info[1]);
                track.write(data, info[3], data.length - info[3]);
            }
            sleep(350);
        } catch (Exception e) {
            Log.w(TAG, "播放出错", e);
        } finally {
            releaseTrack();
            playing = false;
            playThreadOn = false;
            if (!queue.isEmpty() && running) {
                playThreadOn = true;
                new Thread(this::playLoop, "play").start();
            }
        }
    }

    void ensureTrack(int rate, int ch) {
        if (track != null && trackRate == rate) return;
        releaseTrack();
        int min = AudioTrack.getMinBufferSize(rate,
                ch == 2 ? AudioFormat.CHANNEL_OUT_STEREO : AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        track = new AudioTrack(AudioManager.STREAM_MUSIC, rate,
                ch == 2 ? AudioFormat.CHANNEL_OUT_STEREO : AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT, Math.max(min * 2, 16384), AudioTrack.MODE_STREAM);
        track.play();
        trackRate = rate;
    }

    void releaseTrack() {
        try {
            if (track != null) {
                track.stop();
                track.release();
            }
        } catch (Exception ignore) {
        }
        track = null;
        trackRate = 0;
    }

    void stopPlay() {
        queue.clear();
        releaseTrack();
        playing = false;
    }

    /** 只认 16bit PCM 的 wav：返回 {采样率, 声道, 位宽, data 起点} */
    static int[] wavInfo(byte[] b) {
        if (b.length < 44) return null;
        if (!(b[0] == 'R' && b[1] == 'I' && b[2] == 'F' && b[3] == 'F')) return null;
        int p = 12;
        while (p + 8 <= b.length) {
            String id = new String(b, p, 4);
            int size = ByteBuffer.wrap(b, p + 4, 4).order(ByteOrder.LITTLE_ENDIAN).getInt();
            if ("fmt ".equals(id)) {
                int ch = ByteBuffer.wrap(b, p + 10, 2).order(ByteOrder.LITTLE_ENDIAN).getShort();
                int rate = ByteBuffer.wrap(b, p + 12, 4).order(ByteOrder.LITTLE_ENDIAN).getInt();
                int bits = ByteBuffer.wrap(b, p + 22, 2).order(ByteOrder.LITTLE_ENDIAN).getShort();
                if (bits != 16) return null;
                int q = p + 8 + size;
                while (q + 8 <= b.length) {
                    String d = new String(b, q, 4);
                    int ds = ByteBuffer.wrap(b, q + 4, 4).order(ByteOrder.LITTLE_ENDIAN).getInt();
                    if ("data".equals(d)) return new int[]{rate, ch, bits, q + 8};
                    q += 8 + ds + (ds & 1);
                }
                return null;
            }
            p += 8 + size + (size & 1);
        }
        return null;
    }

    // ---------- HTTP ----------

    String post(String u, String body, int timeout) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
        c.setConnectTimeout(timeout);
        c.setReadTimeout(timeout);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        OutputStream os = c.getOutputStream();
        os.write(body.getBytes("UTF-8"));
        os.flush();
        os.close();
        return read(c);
    }

    String get(String u, int timeout) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(u).openConnection();
        c.setConnectTimeout(timeout);
        c.setReadTimeout(timeout);
        return read(c);
    }

    String read(HttpURLConnection c) throws Exception {
        InputStream is = c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream();
        if (is == null) return "";
        BufferedReader br = new BufferedReader(new InputStreamReader(is, "UTF-8"));
        StringBuilder sb = new StringBuilder();
        String line;
        while ((line = br.readLine()) != null) sb.append(line);
        br.close();
        return sb.toString();
    }

    void sendEnd() {
        try {
            get(base + "/voice_edge/end?sid=" + url(sid), 3000);
        } catch (Exception ignore) {
        }
    }

    static String url(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignore) {
        }
    }
}

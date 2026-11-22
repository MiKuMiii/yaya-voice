package com.example.voicelink;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** 极简界面：填地址和口令，点开始；下面显示状态和她说的最后一句。 */
public class MainActivity extends Activity {
    EditText edUrl, edCode;
    TextView tvState, tvText;
    Button btnStart, btnStop;
    final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad, pad, pad);

        edUrl = new EditText(this);
        edUrl.setHint("边缘侧地址，如 http://192.0.2.10:8780");
        edUrl.setText(Config.url(this));

        edCode = new EditText(this);
        edCode.setHint("访问口令");
        edCode.setInputType(InputType.TYPE_CLASS_TEXT);
        edCode.setText(Config.code(this));

        btnStart = new Button(this);
        btnStart.setText("开始通话");

        btnStop = new Button(this);
        btnStop.setText("停止");

        tvState = new TextView(this);
        tvState.setText("待命");
        tvState.setTextSize(20);

        tvText = new TextView(this);
        tvText.setText("（她说的会显示在这儿）");
        tvText.setTextSize(16);

        box.addView(edUrl);
        box.addView(edCode);
        box.addView(btnStart);
        box.addView(btnStop);
        box.addView(tvState);
        box.addView(tvText);
        setContentView(box);

        btnStart.setOnClickListener(v -> {
            if (!hasPerm()) {
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
                return;
            }
            Config.save(this, edUrl.getText().toString(), edCode.getText().toString());
            Intent it = new Intent(this, VoiceService.class);
            it.setAction(VoiceService.ACTION_START);
            startForegroundService(it);
            tvState.setText("连接中……");
        });

        btnStop.setOnClickListener(v -> {
            Intent it = new Intent(this, VoiceService.class);
            it.setAction(VoiceService.ACTION_STOP);
            startService(it);
            tvState.setText("待命");
        });
    }

    boolean hasPerm() {
        if (Build.VERSION.SDK_INT < 23) return true;
        return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] p, int[] r) {
        super.onRequestPermissionsResult(req, p, r);
        if (req == 1 && r.length > 0 && r[0] == PackageManager.PERMISSION_GRANTED) {
            btnStart.performClick();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        ui.post(new Runnable() {
            public void run() {
                tvState.setText(VoiceService.STATUS);
                tvText.setText(VoiceService.LAST_TEXT);
                ui.postDelayed(this, 400);
            }
        });
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacksAndMessages(null);
    }
}

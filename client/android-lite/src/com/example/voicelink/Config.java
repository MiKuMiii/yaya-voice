package com.example.voicelink;

import android.content.Context;
import android.content.SharedPreferences;

/** 两个地址 + 口令，存在 SharedPreferences 里，界面上可改。 */
public class Config {
    public static final String DEF_URL = "http://192.0.2.10:8780";
    public static final String DEF_CODE = "change-me";

    static SharedPreferences sp(Context c) {
        return c.getSharedPreferences("voicelink", Context.MODE_PRIVATE);
    }

    public static String url(Context c) {
        return sp(c).getString("url", DEF_URL);
    }

    public static String code(Context c) {
        return sp(c).getString("code", DEF_CODE);
    }

    public static void save(Context c, String url, String code) {
        sp(c).edit().putString("url", url.trim()).putString("code", code.trim()).apply();
    }
}

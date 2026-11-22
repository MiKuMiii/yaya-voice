# -*- coding: utf-8 -*-
"""一条命令出 APK（不用 Gradle）。

依赖环境变量：
  ANDROID_SDK   Android SDK 根目录（里面有 build-tools/34.0.0 和 platforms/android-34）
  JAVA_HOME     JDK 17
  SEVEN_ZIP     7z 可执行文件（没有就用系统里的 7z / 7za）
  KEYSTORE      .jks 路径；KEYS_PASS 是它的口令（默认 pass:changeit）
"""
import os, shutil, subprocess, sys, glob

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJ = os.path.dirname(os.path.abspath(__file__))
SDK  = os.environ.get("ANDROID_SDK", "")
BT   = os.path.join(SDK, "build-tools", "34.0.0")
JAR  = os.path.join(SDK, "platforms", "android-34", "android.jar")
JDK  = os.path.join(os.environ.get("JAVA_HOME", ""), "bin")
JAVAC = os.path.join(JDK, "javac.exe" if os.name == "nt" else "javac")
JAVA  = os.path.join(JDK, "java.exe" if os.name == "nt" else "java")
AAPT2 = os.path.join(BT, "aapt2.exe" if os.name == "nt" else "aapt2")
ZIP   = os.path.join(BT, "zipalign.exe" if os.name == "nt" else "zipalign")
APKS  = os.path.join(BT, "lib", "apksigner.jar")
D8    = os.path.join(BT, "lib", "d8.jar")
LAMB  = os.path.join(BT, "core-lambda-stubs.jar")
SEVEN = os.environ.get("SEVEN_ZIP", "7z")
KS    = os.environ.get("KEYSTORE", "")
KSPW  = os.environ.get("KEYS_PASS", "pass:changeit")

if not SDK or not os.environ.get("JAVA_HOME"):
    sys.exit("先设 ANDROID_SDK 和 JAVA_HOME")

BUILD = os.path.join(PROJ, "build")
SRC   = os.path.join(PROJ, "src")
RES   = os.path.join(PROJ, "res")
MAN   = os.path.join(PROJ, "AndroidManifest.xml")

def run(args, **kw):
    p = subprocess.run(args, capture_output=True, **kw)
    out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    print("$", os.path.basename(args[0]), "->", p.returncode)
    if out.strip():
        print(out.strip()[:1200])
    if p.returncode != 0:
        sys.exit("FAILED: " + " ".join(args))
    return p

for d in ("gen", "classes", "dex"):
    shutil.rmtree(os.path.join(BUILD, d), ignore_errors=True)
os.makedirs(BUILD, exist_ok=True)

res_zip = os.path.join(BUILD, "res.zip")
if os.path.exists(res_zip): os.remove(res_zip)
run([AAPT2, "compile", "--dir", RES, "-o", res_zip])

unsigned = os.path.join(BUILD, "app-unsigned.apk")
if os.path.exists(unsigned): os.remove(unsigned)
run([AAPT2, "link", "-o", unsigned, "-I", JAR, "--manifest", MAN,
     "--min-sdk-version", "26", "--target-sdk-version", "34",
     "-R", res_zip, "--java", os.path.join(BUILD, "gen"), "--auto-add-overlay"])

srcs = glob.glob(os.path.join(SRC, "**", "*.java"), recursive=True) + \
       glob.glob(os.path.join(BUILD, "gen", "**", "*.java"), recursive=True)
classes = os.path.join(BUILD, "classes"); os.makedirs(classes, exist_ok=True)
run([JAVAC, "-encoding", "UTF-8", "-source", "8", "-target", "8",
     "-bootclasspath", JAR + ";" + LAMB, "-d", classes] + srcs)

dexout = os.path.join(BUILD, "dex"); os.makedirs(dexout, exist_ok=True)
classfiles = glob.glob(os.path.join(classes, "**", "*.class"), recursive=True)
run([JAVA, "-cp", D8, "com.android.tools.r8.D8", "--min-api", "26",
     "--lib", JAR, "--output", dexout] + classfiles)

run([SEVEN, "a", "-tzip", unsigned, os.path.join(dexout, "classes.dex")])

aligned = os.path.join(BUILD, "app-aligned.apk")
if os.path.exists(aligned): os.remove(aligned)
run([ZIP, "-f", "4", unsigned, aligned])

signed = os.path.join(PROJ, "voicelink-debug.apk")
if os.path.exists(signed): os.remove(signed)
run([JAVA, "-jar", APKS, "sign", "--ks", KS,
     "--ks-pass", KSPW, "--key-pass", KSPW,
     "--out", signed, aligned])
run([JAVA, "-jar", APKS, "verify", signed])

print("SIGNED bytes:", os.path.getsize(signed))
data = open(os.path.join(dexout, "classes.dex"), "rb").read()
for s in ["voice_edge", "voice_edge/reply", "wavInfo"]:
    print("dex has", s, ":", s.encode("utf-8") in data)
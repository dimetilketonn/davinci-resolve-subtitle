# -*- coding: utf-8 -*-
"""
Altyazi Uret - DaVinci Resolve entegrasyonu
============================================
Bu script Resolve icinden calisir (Workspace > Scripts > Altyazi Uret):
  1. Aktif timeline'i ses olarak (WAV) render eder
  2. transcribe.py'yi cagirir (faster-whisper)
  3. Uretilen .srt dosyasini Media Pool'a import eder

Kurulum: Bu dosyayi su klasore koy:
  %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility

Asagidaki CONFIG bolumunu kendi yollarina gore duzenle.
"""

import os
import subprocess
import time

# ============================================================
# CONFIG - BURALARI KENDINE GORE DUZENLE
# ============================================================
PYTHON_EXE     = r"C:\Users\<you>\miniconda3\python.exe"   # transcribe.py'yi calistiran Python (faster-whisper'in kurulu oldugu Python)
TRANSCRIBE_PY  = r"C:\path\to\this\repo\transcribe.py"     # transcribe.py'nin tam yolu
WORK_DIR       = r"C:\path\to\this\repo\resolve_temp"      # gecici render/SRT klasoru (otomatik olusturulur)
TRANSLATE      = "none"                                   # "none", "deepl" veya "claude"

# Mod tespiti: "auto"  -> dikey timeline = shorts, yatay = normal (Netflix)
#              "shorts"-> her zaman kelime bazli
#              "normal"-> her zaman Netflix kurallari
MODE           = "auto"

# Altyazilari ileri kaydirma (saniye). Konusma timeline'da gec basliyorsa
# buraya o gecikmeyi yaz (orn. 1.2). 0 = kaydirma yok.
SUBTITLE_OFFSET_SECONDS = 0.0
# ============================================================


def log(msg):
    print("[AltyaziUret] " + str(msg))


def _parse_ts(ts):
    """'00:01:23,456' -> saniye (float)"""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _fmt_ts(sec):
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def shift_srt(src_path, offset_sec):
    """
    SRT'deki tum zamanlari offset kadar kaydirir ve basa, 0'dan ilk cue'ya
    kadar sure dolduran gorunmez (tek bosluk) bir cue ekler. Bu sayede
    AppendToTimeline bastaki boslugu yutamaz. Yeni dosya yolunu dondurur.
    """
    with open(src_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = [b for b in content.strip().split("\n\n") if b.strip()]
    cues = []  # [start, end, text_lines]
    for b in blocks:
        lines = b.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start_s, end_s = [x.strip() for x in lines[1].split("-->")]
        cues.append([_parse_ts(start_s) + offset_sec,
                     _parse_ts(end_s) + offset_sec,
                     lines[2:]])

    if not cues:
        return src_path

    out = []
    idx = 1
    first_start = cues[0][0]
    if first_start > 0.05:
        # Gorunmez dolgu cue: 0 -> ilk cue baslangici
        out.append("%d\n%s --> %s\n%s" % (idx, _fmt_ts(0.0), _fmt_ts(first_start), " "))
        idx += 1

    for start, end, text_lines in cues:
        out.append("%d\n%s --> %s\n%s" % (idx, _fmt_ts(start), _fmt_ts(end), "\n".join(text_lines)))
        idx += 1

    dst_path = src_path.replace(".srt", "_shifted.srt")
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out) + "\n")
    return dst_path


def main():
    resolve = app.GetResolve()  # noqa: F821 (Resolve icinde 'app' hazir gelir)
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if not project:
        log("HATA: Acik proje yok.")
        return

    timeline = project.GetCurrentTimeline()
    if not timeline:
        log("HATA: Aktif timeline yok.")
        return

    tl_name = timeline.GetName()

    # ------------------------------------------------------------
    # MOD TESPITI: dikey timeline -> shorts, yatay -> normal
    # ------------------------------------------------------------
    if MODE == "auto":
        try:
            w = int(timeline.GetSetting("timelineResolutionWidth"))
            h = int(timeline.GetSetting("timelineResolutionHeight"))
            shorts_mode = h > w
            log("Timeline cozunurlugu: %dx%d" % (w, h))
        except Exception as e:
            log("UYARI: Cozunurluk okunamadi (%s), normal mod varsayiliyor." % e)
            shorts_mode = False
    else:
        shorts_mode = (MODE == "shorts")
    log("Mod: " + ("SHORTS (kelime bazli)" if shorts_mode else "NORMAL (Netflix kurallari)"))

    # Dosya adi icin guvenli isim
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in tl_name).strip()
    if not safe_name:
        safe_name = "timeline_audio"

    os.makedirs(WORK_DIR, exist_ok=True)

    # ------------------------------------------------------------
    # 1) SES RENDER
    # ------------------------------------------------------------
    log("Render ayarlari hazirlaniyor: " + tl_name)

    # Onceki render isleri kalmasin diye degil; sadece kendi isimizi takip edecegiz
    ok = project.SetCurrentRenderFormatAndCodec("wav", "lpcm")
    if not ok:
        # Bazi surumlerde codec adi farkli olabiliyor, alternatifleri dene
        ok = project.SetCurrentRenderFormatAndCodec("wav", "LinearPCM")
    log("Format ayari (wav): " + ("OK" if ok else "BASARISIZ - mevcut preset kullanilacak"))

    project.SetRenderSettings({
        "SelectAllFrames": True,        # tum timeline
        "TargetDir": WORK_DIR,
        "CustomName": safe_name,
        "ExportVideo": False,
        "ExportAudio": True,
        "AudioSampleRate": 48000,
        "AudioBitDepth": 16,
    })

    job_id = project.AddRenderJob()
    if not job_id:
        log("HATA: Render job olusturulamadi. Deliver sayfasinda 'Audio Only' preset secip tekrar dene.")
        return

    # Resolve'un bu job icin GERCEKTE kullanacagi yol ve dosya adini ogren
    expected_dir = WORK_DIR
    expected_name = None
    try:
        for j in project.GetRenderJobList():
            if j.get("JobId") == job_id:
                expected_dir = j.get("TargetDir", WORK_DIR)
                expected_name = j.get("OutputFilename")
                log("Job hedefi: %s | dosya: %s" % (expected_dir, expected_name))
                break
    except Exception as e:
        log("UYARI: Job listesi okunamadi: %s" % e)

    log("Render basladi...")
    project.StartRendering(job_id)

    while project.IsRenderingInProgress():
        time.sleep(1)

    # Job durumunu kontrol et
    status = project.GetRenderJobStatus(job_id)
    log("Render durumu: " + str(status.get("JobStatus", "?")))
    if status.get("JobStatus") != "Complete":
        log("HATA: Render tamamlanamadi.")
        return

    # Render kuyrugundan kendi isimizi temizle
    project.DeleteRenderJob(job_id)

    # Once Resolve'un bildirdigi gercek cikti dosyasini dene
    audio_path = None
    if expected_name:
        cand = os.path.join(expected_dir, expected_name)
        if os.path.isfile(cand):
            audio_path = cand

    if not audio_path:
        # Klasordeki en yeni ses/video dosyasini bul (son 10 dk icinde olusan)
        exts = (".wav", ".mp3", ".mov", ".mp4", ".aif", ".aiff", ".flac")
        now = time.time()
        candidates = []
        for d in {expected_dir, WORK_DIR}:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if f.lower().endswith(exts) and now - os.path.getmtime(p) < 600:
                    candidates.append(p)
        if not candidates:
            log("HATA: Render ciktisi bulunamadi.")
            log("Aranan klasor(ler): %s" % " | ".join({expected_dir, WORK_DIR}))
            for d in {expected_dir, WORK_DIR}:
                if os.path.isdir(d):
                    log("  %s icerigi: %s" % (d, os.listdir(d)))
            return
        candidates.sort(key=os.path.getmtime, reverse=True)
        audio_path = candidates[0]

    log("Ses dosyasi hazir: " + audio_path)

    # ------------------------------------------------------------
    # 2) TRANSKRIPSIYON (transcribe.py)
    # ------------------------------------------------------------
    cmd = [PYTHON_EXE, TRANSCRIBE_PY, audio_path, "--output-dir", WORK_DIR]
    if shorts_mode:
        cmd.append("--shorts")
    if TRANSLATE in ("deepl", "claude"):
        cmd += ["--translate", TRANSLATE]

    log("Transkripsiyon basliyor (bu islem GPU'da 1-2 dk surebilir)...")
    log("Komut: " + " ".join(cmd))

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=os.path.dirname(TRANSCRIBE_PY),
    )

    if result.stdout:
        for line in result.stdout.splitlines():
            log("  " + line)
    if result.returncode != 0:
        log("HATA: transcribe.py basarisiz oldu (kod %d)" % result.returncode)
        if result.stderr:
            for line in result.stderr.splitlines()[-15:]:
                log("  STDERR: " + line)
        return

    # ------------------------------------------------------------
    # 3) SRT IMPORT
    # ------------------------------------------------------------
    base = os.path.splitext(os.path.basename(audio_path))[0]
    srt_files = []
    for suffix in ("_tr.srt", "_en.srt"):
        p = os.path.join(WORK_DIR, base + suffix)
        if os.path.isfile(p):
            srt_files.append(p)

    if not srt_files:
        log("HATA: SRT dosyasi bulunamadi: " + WORK_DIR)
        return

    # Offset uygula ve bastaki boslugu dolgu cue ile koru
    if SUBTITLE_OFFSET_SECONDS != 0:
        log("Altyazilar %.2f sn kaydiriliyor..." % SUBTITLE_OFFSET_SECONDS)
    srt_files = [shift_srt(p, SUBTITLE_OFFSET_SECONDS) for p in srt_files]

    media_pool = project.GetMediaPool()
    imported = media_pool.ImportMedia(srt_files)
    if not imported:
        log("UYARI: Import basarisiz olabilir, SRT yine de burada: " + srt_files[0])
        log("TAMAMLANDI ✔")
        return

    log("SRT Media Pool'a eklendi: " + ", ".join(os.path.basename(s) for s in srt_files))

    # ------------------------------------------------------------
    # 4) TIMELINE'A OTOMATIK YERLESTIRME DENEMESI
    # (Ucretsiz surumde her zaman calismayabilir; basarisizsa manuel adim gerekir)
    # ------------------------------------------------------------
    auto_ok = False
    try:
        def subtitle_item_count(tl):
            total = 0
            try:
                n = tl.GetTrackCount("subtitle")
                for i in range(1, int(n) + 1):
                    items = tl.GetItemListInTrack("subtitle", i)
                    total += len(items) if items else 0
            except Exception:
                pass
            return total

        before = subtitle_item_count(timeline)
        media_pool.AppendToTimeline(imported)
        time.sleep(0.5)
        after = subtitle_item_count(project.GetCurrentTimeline() or timeline)
        auto_ok = after > before
    except Exception as e:
        log("Otomatik yerlestirme denemesi hata verdi: %s" % e)

    if auto_ok:
        log("Altyazilar timeline'a otomatik eklendi (%d cue)." % after)
    else:
        log("Otomatik yerlestirme bu surumde olmadi.")
        log("Manuel adim: Media Pool'daki SRT'ye sag tik > 'Insert Selected Subtitles to Timeline'.")

    log("TAMAMLANDI ✔")


main()

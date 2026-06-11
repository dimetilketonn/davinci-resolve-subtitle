"""
YouTube içerikleri için lokal altyazı üretici
- faster-whisper ile Türkçe transkripsiyon (RTX 3060 için optimize)
- DeepL veya Claude API ile İngilizce çeviri
- Hem normal videolar hem de Shorts için iki farklı altyazı modu

Kurulum:
    pip install faster-whisper deepl anthropic

Kullanım:
    python subtitle_generator.py input_audio.mp3
    python subtitle_generator.py input_audio.mp3 --shorts   # kelime bazlı altyazı
    python subtitle_generator.py input_audio.mp3 --translate deepl
"""

import argparse
import os
import sys
from pathlib import Path

# Windows'ta nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip paketlerinin DLL'lerini yükle
if sys.platform == "win32":
    import site
    for sp in site.getsitepackages():
        for sub in (r"nvidia\cublas\bin", r"nvidia\cudnn\bin"):
            p = Path(sp) / sub
            if p.is_dir():
                os.add_dll_directory(str(p))
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")

from faster_whisper import WhisperModel


# ---------- YARDIMCI FONKSİYONLAR ----------

def format_timestamp(seconds: float) -> str:
    """Saniyeyi SRT formatına çevirir: 00:01:23,456"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    millis = int(round((secs - int(secs)) * 1000))
    return f"{hours:02d}:{minutes:02d}:{int(secs):02d},{millis:03d}"


def write_srt(segments, output_path: str):
    """Segment listesinden SRT dosyası yazar"""
    with open(output_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}\n")
            f.write(f"{seg['text'].strip()}\n\n")



def detect_speech_regions(audio_path: str) -> list:
    """Silero VAD ile gercek konusma bolgelerini [(start, end), ...] saniye olarak dondurur."""
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        audio = decode_audio(audio_path, sampling_rate=16000)
        try:
            opts = VadOptions(threshold=0.7, min_silence_duration_ms=150, speech_pad_ms=0)
            ts = get_speech_timestamps(audio, vad_options=opts)
        except TypeError:
            ts = get_speech_timestamps(audio)
        return [(t["start"] / 16000.0, t["end"] / 16000.0) for t in ts]
    except Exception as e:
        print(f"[!] VAD bolge tespiti basarisiz, atlaniyor: {e}")
        return []


def snap_segments_to_vad(segments: list, regions: list, tolerance: float = 0.12,
                         lead_guard: float = 0.15, max_shift: float = 1.0) -> list:
    """
    Whisper segmentleri gercek konusmadan once basliyorsa (whisper'in bilinen
    erken baslatma egilimi), baslangiclarini VAD'in buldugu gercek konusma
    baslangicina ceker. Kelime zamanlarini da orantili yeniden olcekler.

    - lead_guard: segment basindaki cok kisa konusma kuyruklarini yok sayar,
      boylece nefes/kuyruk sesleri yuzunden duzeltme atlanmaz.
    - max_shift: guvenlik siniri; bir segmenti 1 saniyeden fazla kaydirmaz.
    """
    if not segments or not regions:
        return segments
    fixed = 0
    for seg in segments:
        target = None
        for r_start, r_end in regions:
            if r_end > seg["start"] + lead_guard:
                target = r_start
                break
        if target is None:
            continue
        shift = target - seg["start"]
        if tolerance < shift <= max_shift and target < seg["end"]:
            old_start, end = seg["start"], seg["end"]
            scale = (end - target) / (end - old_start) if end > old_start else 1.0
            for w in seg.get("words") or []:
                w["start"] = target + (w["start"] - old_start) * scale
                w["end"] = target + (w["end"] - old_start) * scale
            print(f"[*] Segment duzeltildi: {format_timestamp(old_start)} -> {format_timestamp(target)}")
            seg["start"] = target
            fixed += 1
    if fixed == 0:
        print("[*] VAD kontrolu: tum segmentler zaten hizali")
    return segments


# ---------- TRANSKRİPSİYON ----------

def transcribe(audio_path: str, model_size: str = "large-v3", language: str = "tr",
               compute_type: str = "int8_float16", device: str = "cuda"):
    """
    Sesi transkribe eder ve segment listesi döner.

    VRAM rehberi:
    - 12GB+ : compute_type="float16"        (en kaliteli)
    - 6-8GB : compute_type="int8_float16"   (kalite farkı minimal, large-v3 sığar)
    - CPU   : device="cpu", compute_type="int8"
    """
    print(f"[*] Model yükleniyor: {model_size} (device={device}, compute={compute_type})")

    model = WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
    )
    
    print(f"[*] Transkripsiyon başlıyor: {audio_path}")
    
    segments_gen, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,                    # Kalite/hız dengesi
        vad_filter=True,                # Sessizlikleri filtrele (halüsinasyon önler)
        vad_parameters=dict(
            min_silence_duration_ms=500,
            threshold=0.5
        ),
        word_timestamps=True,           # Kelime bazlı zaman damgası (Shorts için lazım)
        condition_on_previous_text=False,  # Halüsinasyon riskini azaltır
        temperature=0.0,                # Deterministik çıktı
        no_speech_threshold=0.6,        # Sessizlik tespiti
    )
    
    # Generator'ı listeye çevir, kelimeleri de sakla
    segments = []
    for seg in segments_gen:
        words = []
        if seg.words:
            for w in seg.words:
                words.append({
                    "start": w.start,
                    "end": w.end,
                    "word": w.word
                })
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text,
            "words": words
        })
        # Anlık çıktı göster
        print(f"  [{format_timestamp(seg.start)}] {seg.text.strip()[:80]}")
    
    print(f"[+] {len(segments)} segment üretildi. Tespit edilen dil: {info.language} ({info.language_probability:.2f})")

    # Whisper'in segmentleri erken baslatma egilimini VAD ile duzelt
    regions = detect_speech_regions(audio_path)
    if regions:
        print(f"[*] VAD: {len(regions)} konusma bolgesi (ilki {format_timestamp(regions[0][0])})")
        segments = snap_segments_to_vad(segments, regions)
    else:
        print("[!] VAD bolgesi bulunamadi, zaman duzeltmesi atlandi")

    return segments


# ---------- NETFLIX STANDARDI ALTYAZI BÖLME ----------

# Netflix Timed Text Style Guide (TR/EN ortak)
NETFLIX_MAX_CHARS_PER_LINE = 42
NETFLIX_MAX_LINES = 2          # => max 84 karakter / cue
NETFLIX_MAX_DURATION = 7.0     # saniye
NETFLIX_MIN_DURATION = 1.0     # saniye (çok kısa kalırsa uzat)
NETFLIX_MAX_CPS = 17           # karakter / saniye okuma hızı

# Cümle sonu kabul edilen noktalar (öncelikli kırılma)
SENTENCE_END = (".", "!", "?", "…")
# İkincil kırılma noktaları
SOFT_BREAK = (",", ";", ":", "—", "-")


def _format_two_lines(text: str) -> str:
    """Tek satıra sığmıyorsa ortadan dengeli iki satıra böl."""
    text = text.strip()
    if len(text) <= NETFLIX_MAX_CHARS_PER_LINE:
        return text

    words = text.split()
    # Ortaya en yakın boşlukta böl (iki satır mümkün olduğunca dengeli olsun)
    best_idx = None
    best_diff = float("inf")
    for i in range(1, len(words)):
        left = " ".join(words[:i])
        right = " ".join(words[i:])
        if len(left) > NETFLIX_MAX_CHARS_PER_LINE or len(right) > NETFLIX_MAX_CHARS_PER_LINE:
            continue
        diff = abs(len(left) - len(right))
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    if best_idx is None:
        # İki satıra sığmıyor (çok uzun) — yine de ortadan kır, üst akış zaten chunk'ı parçalayacak
        mid = len(words) // 2
        return " ".join(words[:mid]) + "\n" + " ".join(words[mid:])

    return " ".join(words[:best_idx]) + "\n" + " ".join(words[best_idx:])


def _pick_split_index(words: list, max_chars: int) -> int:
    """
    Kelime listesi içinde nereden bölüneceğini seçer.
    Öncelik sırası:
      1) Cümle sonu noktalama (max_chars'a kadar olan en son)
      2) Yumuşak noktalama (virgül vb.)
      3) En uzun kelime arası duraklama (gap)
      4) Karakter limitine en yakın kelime sınırı
    Döndürür: bu index'e KADAR (dahil) ilk parçaya gider.
    """
    cumulative = 0
    sentence_end_idx = None
    soft_break_idx = None
    biggest_gap = -1.0
    biggest_gap_idx = None
    last_fitting_idx = 0

    for i, w in enumerate(words):
        token = w["word"]
        cumulative += len(token)
        if cumulative > max_chars and i > 0:
            break
        last_fitting_idx = i

        stripped = token.rstrip()
        if stripped.endswith(SENTENCE_END):
            sentence_end_idx = i
        elif stripped.endswith(SOFT_BREAK):
            soft_break_idx = i

        if i + 1 < len(words):
            gap = words[i + 1]["start"] - w["end"]
            if gap > biggest_gap:
                biggest_gap = gap
                biggest_gap_idx = i

    if sentence_end_idx is not None:
        return sentence_end_idx
    if soft_break_idx is not None:
        return soft_break_idx
    if biggest_gap_idx is not None and biggest_gap > 0.15:
        return biggest_gap_idx
    return last_fitting_idx


def split_segment_netflix(seg: dict) -> list:
    """
    Tek bir Whisper segmentini Netflix kurallarına göre alt-cue'lara böler.
    Kelime timestamp'leri varsa onları kullanır, yoksa orijinal segmenti döner.
    """
    words = seg.get("words") or []
    text = seg["text"].strip()
    duration = seg["end"] - seg["start"]

    # Yeterince kısa ve hızlı okunabiliyorsa olduğu gibi bırak
    cps = len(text) / duration if duration > 0 else 0
    if (
        len(text) <= NETFLIX_MAX_CHARS_PER_LINE * NETFLIX_MAX_LINES
        and duration <= NETFLIX_MAX_DURATION
        and cps <= NETFLIX_MAX_CPS
        and not words  # kelime yoksa zaten bölemeyiz
    ):
        return [{"start": seg["start"], "end": seg["end"], "text": text}]

    if not words:
        return [{"start": seg["start"], "end": seg["end"], "text": text}]

    # Kelime bazlı bölme
    cues = []
    remaining = list(words)
    max_chars_per_cue = NETFLIX_MAX_CHARS_PER_LINE * NETFLIX_MAX_LINES

    while remaining:
        # Süre limitini de gözet — uzun süreli cue olmasın
        # max_chars'ı süre kısıtına göre dinamik daralt
        first_start = remaining[0]["start"]
        # Süre sınırına kadar kaç kelime sığar bul
        words_in_window = []
        for w in remaining:
            if w["end"] - first_start > NETFLIX_MAX_DURATION:
                break
            words_in_window.append(w)
        if not words_in_window:
            words_in_window = [remaining[0]]

        cut = _pick_split_index(words_in_window, max_chars_per_cue)
        chunk = remaining[: cut + 1]
        remaining = remaining[cut + 1 :]

        chunk_text = "".join(w["word"] for w in chunk).strip()
        cue_start = chunk[0]["start"]
        cue_end = chunk[-1]["end"]

        # Min süre garantisi
        if cue_end - cue_start < NETFLIX_MIN_DURATION and remaining:
            cue_end = min(cue_start + NETFLIX_MIN_DURATION, remaining[0]["start"])

        cues.append({
            "start": cue_start,
            "end": cue_end,
            "text": _format_two_lines(chunk_text),
        })

    return cues


def apply_netflix_rules(segments: list) -> list:
    """Tüm segmentlere Netflix bölme kurallarını uygular."""
    out = []
    for seg in segments:
        out.extend(split_segment_netflix(seg))
    return out


# ---------- SHORTS İÇİN KELİME BAZLI ALTYAZI ----------

def make_word_chunks(segments, max_words: int = 4, max_duration: float = 2.0,
                     lead: float = 0.12, min_duration: float = 0.55,
                     snap_gap: float = 0.40):
    """
    Shorts/Reels stili altyazı için segmentleri kelime kelime parçalar.
    Her satıra max_words kadar kelime, max_duration saniye sınırı.

    lead         : her chunk'ı bu kadar saniye erken başlat (Whisper genelde
                   word-start'ı biraz geç işaretler; gözle hizalama daha doğru olur)
    min_duration : çok kısa flaş-cue olmasın diye alt sınır
    snap_gap     : iki chunk arası bu süreden az ise sonrakini öne çekip
                   öncekinin sonuna yapıştır (boş kareleri yok et)
    """
    raw = []
    current_words = []
    # Apostrof/noktalama ile başlayan tokenları önceki kelimeye yapıştır
    GLUE_PREFIXES = ("'", "’", ".", ",", "!", "?", ":", ";", "…", "”", "\"", ")")

    for seg in segments:
        for word in seg["words"]:
            if not current_words:
                current_words.append(word)
                continue
            stripped = word["word"].lstrip()
            glue = stripped.startswith(GLUE_PREFIXES)
            duration = word["end"] - current_words[0]["start"]
            if not glue and (len(current_words) >= max_words or duration >= max_duration):
                raw.append({
                    "start": current_words[0]["start"],
                    "end": current_words[-1]["end"],
                    "text": "".join(w["word"] for w in current_words).strip(),
                })
                current_words = [word]
            else:
                current_words.append(word)

    if current_words:
        raw.append({
            "start": current_words[0]["start"],
            "end": current_words[-1]["end"],
            "text": "".join(w["word"] for w in current_words).strip(),
        })

    # Hassasiyet rötuşları
    SENT_END = (".", "!", "?", "…")
    for i, c in enumerate(raw):
        prev = raw[i - 1] if i > 0 else None
        prev_end = prev["end"] if prev else 0.0
        prev_sent_end = bool(prev) and prev["text"].rstrip().endswith(SENT_END)
        gap = c["start"] - prev_end
        # Cümle sonrası gerçek sessizliği koru: büyük boşluk ya da nokta varsa lead'i kullanma
        if prev_sent_end and gap > 0.25:
            pass  # start'a dokunma
        else:
            c["start"] = max(prev_end, c["start"] - lead)
        # Min süre
        if c["end"] - c["start"] < min_duration:
            c["end"] = c["start"] + min_duration

    # Snap-to-next: küçük boşlukları kapat (cümle sonu sonrası snap yok)
    for i in range(len(raw) - 1):
        gap = raw[i + 1]["start"] - raw[i]["end"]
        sent_end = raw[i]["text"].rstrip().endswith(SENT_END)
        if 0 < gap <= snap_gap and not sent_end:
            raw[i]["end"] = raw[i + 1]["start"]

    return raw


# ---------- TOPLANTI MODU (Diarization + Döküm) ----------

def diarize(audio_path: str, hf_token: str, device: str = "cuda"):
    """
    pyannote.audio ile konuşmacı ayrımı.
    Dönen: [{"start": float, "end": float, "speaker": "SPEAKER_00"}, ...]
    """
    from pyannote.audio import Pipeline
    import torch

    print("[*] Diarization modeli yükleniyor (pyannote/speaker-diarization-3.1)...")
    try:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token,
        )
    except TypeError:
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )
    if device == "cuda" and torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))

    print(f"[*] Diarization başlıyor: {audio_path}")
    # torchcodec'i bypass etmek için sesi tensor olarak yükle
    import soundfile as sf
    import numpy as np
    waveform_np, sample_rate = sf.read(audio_path, always_2d=True)
    # (samples, channels) -> (channels, samples), float32
    waveform = torch.from_numpy(waveform_np.T.astype(np.float32))
    diarization = pipeline({"waveform": waveform, "sample_rate": sample_rate})
    # pyannote 4.x: DiarizeOutput; 3.x: Annotation
    annotation = getattr(diarization, "exclusive_speaker_diarization", None) \
        or getattr(diarization, "speaker_diarization", diarization)
    turns = []
    for turn, _, speaker in annotation.itertracks(yield_label=True):
        turns.append({"start": turn.start, "end": turn.end, "speaker": speaker})
    speakers = sorted({t["speaker"] for t in turns})
    print(f"[+] {len(turns)} konuşma parçası, {len(speakers)} farklı konuşmacı bulundu: {', '.join(speakers)}")
    return turns


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speakers(segments: list, turns: list) -> list:
    """Her whisper segmentine, en çok örtüşen diarization turn'ünün konuşmacısını ata."""
    out = []
    for seg in segments:
        best_speaker = None
        best_overlap = 0.0
        for t in turns:
            ov = _overlap(seg["start"], seg["end"], t["start"], t["end"])
            if ov > best_overlap:
                best_overlap = ov
                best_speaker = t["speaker"]
        out.append({**seg, "speaker": best_speaker or "BİLİNMİYOR"})
    return out


def format_meeting_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_meeting_transcript(segments_with_speakers: list, output_path: str):
    """
    Ardışık aynı konuşmacının segmentlerini birleştirir,
    markdown formatında konuşmacı bloklarıyla yazar.
    Konuşmacılar SPEAKER_00 -> Konuşmacı A, SPEAKER_01 -> Konuşmacı B ...
    """
    # Konuşmacı etiketlerini A, B, C ... olarak görünüş sırasına göre eşle
    label_map = {}
    next_label_idx = 0
    for seg in segments_with_speakers:
        sp = seg["speaker"]
        if sp not in label_map:
            label_map[sp] = chr(ord("A") + next_label_idx) if next_label_idx < 26 else sp
            next_label_idx += 1

    # Ardışık aynı konuşmacı bloklarını birleştir
    blocks = []
    for seg in segments_with_speakers:
        speaker = label_map[seg["speaker"]]
        text = seg["text"].strip()
        if blocks and blocks[-1]["speaker"] == speaker:
            blocks[-1]["end"] = seg["end"]
            blocks[-1]["text"] += " " + text
        else:
            blocks.append({
                "speaker": speaker,
                "start": seg["start"],
                "end": seg["end"],
                "text": text,
            })

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# Toplantı Dökümü\n\n")
        for sp_orig, sp_label in label_map.items():
            f.write(f"- **Konuşmacı {sp_label}** ({sp_orig})\n")
        f.write("\n---\n\n")
        for b in blocks:
            ts = format_meeting_timestamp(b["start"])
            f.write(f"### Konuşmacı {b['speaker']} — [{ts}]\n\n{b['text']}\n\n")


# ---------- ÇEVİRİ ----------

def translate_with_deepl(segments, target_lang: str = "EN-US"):
    """DeepL API ile çeviri. DEEPL_API_KEY environment variable'ı gerekli."""
    import deepl
    
    api_key = os.environ.get("DEEPL_API_KEY")
    if not api_key:
        raise ValueError("DEEPL_API_KEY environment variable'ı tanımlı değil")
    
    translator = deepl.Translator(api_key)
    
    print(f"[*] DeepL ile {len(segments)} segment çevriliyor...")
    translated = []
    for seg in segments:
        result = translator.translate_text(
            seg["text"].strip(),
            source_lang="TR",
            target_lang=target_lang
        )
        translated.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": result.text
        })
    
    return translated


def translate_with_claude(segments, context: str = ""):
    """
    Claude API ile bağlam-aware çeviri.
    Avantajı: Deyimleri, esprileri, kanal tarzını koruyabilir.
    ANTHROPIC_API_KEY environment variable gerekli.
    """
    from anthropic import Anthropic
    client = Anthropic()
    
    # Tüm metni tek seferde çevirip tutarlılık sağla
    full_text = "\n".join(f"[{i}] {seg['text'].strip()}" for i, seg in enumerate(segments))
    
    prompt = f"""Aşağıdaki Türkçe altyazıları doğal ve akıcı bir İngilizceye çevir.
Her satırın başındaki [numara] etiketini koru. Konuşma dili tarzını koru, fazla resmi olma.
{context}

Türkçe altyazılar:
{full_text}

Sadece çevirileri ver, başka açıklama yapma."""
    
    print(f"[*] Claude ile çeviri yapılıyor...")
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )
    
    # Çıktıyı parse et
    output = response.content[0].text
    translated_lines = {}
    for line in output.strip().split("\n"):
        if line.startswith("[") and "]" in line:
            idx_str = line[1:line.index("]")]
            try:
                idx = int(idx_str)
                text = line[line.index("]")+1:].strip()
                translated_lines[idx] = text
            except ValueError:
                continue
    
    translated = []
    for i, seg in enumerate(segments):
        translated.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": translated_lines.get(i, seg["text"])
        })
    
    return translated


# ---------- ANA AKIŞ ----------

def main():
    parser = argparse.ArgumentParser(description="Lokal altyazı üretici")
    parser.add_argument("audio", help="Ses veya video dosyası yolu")
    parser.add_argument("--model", default="large-v3", help="Whisper model boyutu")
    parser.add_argument("--language", default="tr", help="Kaynak dil")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"])
    parser.add_argument("--compute-type", default="int8_float16",
                        choices=["float16", "int8_float16", "int8", "float32"],
                        help="6GB VRAM => int8_float16, 12GB+ => float16, CPU => int8")
    parser.add_argument("--shorts", action="store_true", help="Shorts için kelime bazlı altyazı")
    parser.add_argument("--meeting", action="store_true",
                        help="Toplantı modu: konuşmacı ayrımı + markdown döküm")
    parser.add_argument("--hf-token", default=None,
                        help="HuggingFace token (pyannote modeli için). Yoksa HF_TOKEN env var okunur.")
    parser.add_argument("--translate", choices=["deepl", "claude", "none"], default="none",
                        help="İngilizceye çeviri yöntemi")
    parser.add_argument("--output-dir", default=".", help="Çıktı klasörü")
    args = parser.parse_args()
    
    audio_path = Path(args.audio)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True)
    base_name = audio_path.stem
    
    # 1. Transkripsiyon
    segments = transcribe(str(audio_path), model_size=args.model, language=args.language,
                          compute_type=args.compute_type, device=args.device)

    # Toplantı modu: diarization + markdown döküm, SRT üretme
    if args.meeting:
        hf_token = args.hf_token or os.environ.get("HF_TOKEN")
        if not hf_token:
            print("[!] HF token bulunamadı. --hf-token veya HF_TOKEN env var ayarla.")
            sys.exit(1)
        turns = diarize(str(audio_path), hf_token=hf_token, device=args.device)
        labeled = assign_speakers(segments, turns)
        md_path = output_dir / f"{base_name}_dokum.md"
        write_meeting_transcript(labeled, str(md_path))
        print(f"[+] Toplantı dökümü kaydedildi: {md_path}")
        return

    # 2. Shorts modu - kelime bazlı parçalama; normal modda Netflix kurallarına göre böl
    if args.shorts:
        display_segments = make_word_chunks(segments, max_words=4, max_duration=1.5)
    else:
        display_segments = apply_netflix_rules(segments)
    
    # 3. Türkçe SRT yaz
    tr_path = output_dir / f"{base_name}_tr.srt"
    write_srt(display_segments, str(tr_path))
    print(f"[+] Türkçe altyazı kaydedildi: {tr_path}")
    
    # 4. Çeviri (opsiyonel)
    if args.translate == "deepl":
        en_segments = translate_with_deepl(display_segments)
        en_path = output_dir / f"{base_name}_en.srt"
        write_srt(en_segments, str(en_path))
        print(f"[+] İngilizce altyazı kaydedildi: {en_path}")
    elif args.translate == "claude":
        en_segments = translate_with_claude(display_segments)
        en_path = output_dir / f"{base_name}_en.srt"
        write_srt(en_segments, str(en_path))
        print(f"[+] İngilizce altyazı kaydedildi: {en_path}")


if __name__ == "__main__":
    main()
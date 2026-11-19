import json
import os
import random
import re
import subprocess
import time
import wave
import cv2
import numpy as np
import requests
import cloudinary
import cloudinary.uploader
from faster_whisper import WhisperModel
from google import genai

CLOUDINARY_URL = os.environ["CLOUDINARY_URL"]
BUFFER_ACCESS_TOKEN = os.environ["BUFFER_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")
COOKIES_FILE = "cookies.txt"
HISTORY_FILE = "posted_history.json"

cloudinary.config(cloudinary_url=CLOUDINARY_URL)

if YOUTUBE_COOKIES.strip():
    with open(COOKIES_FILE, "w", encoding="utf-8") as cf:
        cf.write(YOUTUBE_COOKIES.strip() + "\n")

STREAMERS = [
    {"name": "KaiCenat", "platform": "Twitch", "url": "https://www.twitch.tv/kaicenat/clips?filter=clips&range=24hr", "fallback_cam": "top-left"},
    {"name": "xQc", "platform": "Twitch", "url": "https://www.twitch.tv/xqc/clips?filter=clips&range=24hr", "fallback_cam": "gta-left"},
    {"name": "CaseOh", "platform": "Twitch", "url": "https://www.twitch.tv/caseoh_/clips?filter=clips&range=24hr", "fallback_cam": "mid-left"},
    {"name": "IShowSpeed", "platform": "YouTube", "url": "https://www.youtube.com/@IShowSpeed/streams", "fallback_cam": "top-left"},
    {"name": "IShowSpeed", "platform": "YouTube", "url": "ytsearch8:IShowSpeed live stream clip", "fallback_cam": "top-left"},
]

GTA6_KEYWORDS = ("gta 6", "gta6", "gta vi", "gtavi", "grand theft auto vi", "grand theft auto 6")

UNSAFE_KEYWORDS = (
    "pegging", "porn", "nsfw", "naked", "nude", "nudity", "sex", "sexual",
    "strip club", "stripper", "onlyfans", "hentai", "blowjob", "handjob",
    "erotic", "bdsm", "fetish", "slut", "whore", "rape", "suicide", "gore"
)


def yt_auth_args():
    args = ["--js-runtimes", "node"]
    if os.path.exists(COOKIES_FILE):
        args.extend(["--cookies", COOKIES_FILE])
    else:
        args.extend(["--extractor-args", "youtube:player_client=android,ios"])
    return args


def is_safe_text(text):
    lowered = text.lower()
    return not any(re.search(rf"\b{re.escape(bad)}\b", lowered) for bad in UNSAFE_KEYWORDS)


def is_gta6_entry(entry):
    text = " ".join([
        str(entry.get("title", "")),
        str(entry.get("description", "")),
        " ".join(entry.get("categories") or []),
        " ".join(entry.get("tags") or []),
    ]).lower()
    return any(k in text for k in GTA6_KEYWORDS)


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("posted_ids", [])
                data.setdefault("last_streamer", "")
                data.setdefault("last_platform", "")
                return data
        except Exception:
            pass
    return {"posted_ids": [], "last_streamer": "", "last_platform": ""}


def save_history(history):
    history["posted_ids"] = history["posted_ids"][-300:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def collect_viral_candidates(streamers, history):
    candidates = []
    last_streamer = history.get("last_streamer", "")
    last_platform = history.get("last_platform", "")

    for streamer in streamers:
        url = streamer["url"]
        print(f"Scanning {streamer['name']} [{streamer['platform']}] ({url})...")
        probe_cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", "15"]
        if streamer["platform"] == "YouTube":
            probe_cmd.extend(yt_auth_args())
        probe_cmd.append(url)

        try:
            probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            print(f"Probe timed out for {streamer['name']}, skipping...")
            continue

        if probe.returncode != 0 or not probe.stdout.strip():
            print(f"Probe returned empty for {streamer['name']}: {probe.stderr[-180:]}")
            continue

        try:
            info = json.loads(probe.stdout)
            entries = info.get("entries") or [info]
            for entry in entries:
                if not entry or entry.get("live_status") == "is_upcoming":
                    continue
                t_url = entry.get("webpage_url") or entry.get("url")
                v_id = entry.get("id")
                title = entry.get("title", "")
                if not t_url or not v_id:
                    continue

                if not is_safe_text(title):
                    print(f"Skipping unsafe title for YouTube TOS: {title[:60]}")
                    continue

                is_live = (entry.get("live_status") == "is_live")
                history_id = f"{v_id}_{int(time.time() // 3600)}" if is_live else v_id
                if history_id in history["posted_ids"]:
                    continue

                if t_url.startswith("/") or not t_url.startswith("http"):
                    t_url = f"https://www.youtube.com/watch?v={v_id}"

                raw_views = int(entry.get("view_count") or 0)
                views = raw_views if raw_views > 0 else (8000 if is_live else 4000)
                gta6_match = is_gta6_entry(entry)
                is_different_creator = (streamer["name"] != last_streamer)
                is_different_platform = (streamer["platform"] != last_platform)

                candidates.append({
                    "streamer": streamer,
                    "url": t_url,
                    "id": history_id,
                    "views": views,
                    "is_live": is_live,
                    "is_gta6": gta6_match,
                    "different_creator": is_different_creator,
                    "different_platform": is_different_platform,
                    "title": title,
                })
        except Exception as e:
            print(f"Parse warning for {streamer['name']}: {e}")

    for c in candidates:
        c["weighted_views"] = c["views"] * random.uniform(0.85, 1.15)

    candidates.sort(
        key=lambda c: (c["is_gta6"], c["different_platform"], c["different_creator"], c["weighted_views"]),
        reverse=True
    )
    print(f"Total safe unposted candidates found: {len(candidates)}")
    return candidates


def download_candidate(candidate, raw_output="raw_stream.mp4"):
    if os.path.exists(raw_output):
        os.remove(raw_output)

    target_url = candidate["url"]
    name = candidate["streamer"]["name"]
    platform = candidate["streamer"]["platform"]
    is_live = candidate.get("is_live", False)
    print(f"Downloading candidate: {name} [{platform}] | {candidate['views']:,} views | Live={is_live} | GTA6={candidate['is_gta6']} | {candidate['title'][:60]}")

    is_twitch_clip = "twitch.tv" in target_url and "/clip/" in target_url
    yt_flags = yt_auth_args() if platform == "YouTube" else []

    if is_twitch_clip:
        cmd = [
            "yt-dlp", "--no-playlist",
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", raw_output, target_url
        ]
    elif is_live:
        cmd = [
            "yt-dlp", "--no-playlist",
            *yt_flags,
            "--live-from-start",
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "--merge-output-format", "mp4",
            "--download-sections", "*00:10:00-00:11:30",
            "-o", raw_output, target_url
        ]
    else:
        start_min = random.randint(15, 55)
        end_min = start_min + 2
        section = f"*00:{start_min:02d}:00-00:{end_min:02d}:00"
        cmd = [
            "yt-dlp", "--no-playlist",
            *yt_flags,
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "--merge-output-format", "mp4",
            "--download-sections", section,
            "-o", raw_output, target_url
        ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=150)
        if os.path.exists(raw_output) and os.path.getsize(raw_output) > 250_000:
            cap = cv2.VideoCapture(raw_output)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            if w <= h:
                print(f"Skipping vertical/pre-edited clip ({w}x{h}) for {name}...")
                return "skip_permanently"
            return raw_output
        print(f"Download failed for {name}: {res.stderr[-250:]}")
    except subprocess.TimeoutExpired:
        print(f"Download timed out for {name}")
        if os.path.exists(raw_output):
            os.remove(raw_output)

    return None


def find_hype_window(video_path, clip_len=38):
    wav_path = "temp_audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-ac", "1", "-ar", "16000", wav_path],
        capture_output=True, check=True
    )

    with wave.open(wav_path, "rb") as wf:
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32)

    total_sec = len(samples) / rate
    if total_sec <= clip_len + 4:
        return 0.0, max(5.0, total_sec)

    sec_energies = []
    for s in range(int(total_sec)):
        chunk = samples[s * rate : (s + 1) * rate]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) > 0 else 0.0
        sec_energies.append(rms)

    peak_sec = int(np.argmax(sec_energies))
    start_sec = max(0.0, min(float(peak_sec - 6), total_sec - clip_len))
    return start_sec, clip_len


def generate_word_subtitles(clip_audio_path, ass_path="subs.ass"):
    model = WhisperModel("base.en", device="cpu", compute_type="int8")

    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,1215,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    lines = [ass_header]
    transcript_parts = []

    try:
        segments, _ = model.transcribe(clip_audio_path, word_timestamps=True, vad_filter=True)
        words = []
        for seg in segments:
            if seg.words:
                for w in seg.words:
                    words.append((w.word.strip(), w.start, w.end))

        chunk_size = 3
        for i in range(0, len(words), chunk_size):
            group = words[i : i + chunk_size]
            phrase = " ".join(w[0] for w in group).upper()
            phrase = re.sub(r"[^\w\s'!?.,-]", "", phrase)
            if not phrase:
                continue
            start_t = fmt_time(group[0][1])
            end_sec = min(group[-1][2] + 0.25, group[0][1] + 2.2)
            end_t = fmt_time(max(end_sec, group[0][1] + 0.3))
            lines.append(f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{phrase}")
            transcript_parts.append(phrase)
    except Exception as e:
        print(f"Word-level alignment skipped ({e}); falling back to segment captions.")
        try:
            segments, _ = model.transcribe(clip_audio_path, word_timestamps=False, vad_filter=True)
            for seg in segments:
                phrase = re.sub(r"[^\w\s'!?.,-]", "", seg.text.strip().upper())
                if phrase:
                    lines.append(f"Dialogue: 0,{fmt_time(seg.start)},{fmt_time(seg.end)},Default,,0,0,0,,{phrase}")
                    transcript_parts.append(phrase)
        except Exception:
            pass

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return ass_path, " ".join(transcript_parts)


def detect_facecam_box(video_path):
    if not hasattr(cv2, "CascadeClassifier"):
        return None

    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if total_frames <= 0 or w <= 0 or h <= 0:
        cap.release()
        return None

    cascade_dir = getattr(getattr(cv2, "data", None), "haarcascades", "")
    frontal = cv2.CascadeClassifier(cascade_dir + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(cascade_dir + "haarcascade_profileface.xml")
    if frontal.empty():
        cap.release()
        return None

    detections = []
    for frac in (0.08, 0.20, 0.32, 0.44, 0.56, 0.68, 0.80, 0.92):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total_frames * frac))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        faces = list(frontal.detectMultiScale(gray, scaleFactor=1.10, minNeighbors=5, minSize=(36, 36)))
        if not faces and not profile.empty():
            faces = list(profile.detectMultiScale(gray, scaleFactor=1.10, minNeighbors=5, minSize=(36, 36)))

        for (fx, fy, fw, fh) in faces:
            cx, cy = fx + fw / 2.0, fy + fh / 2.0
            if cx < w * 0.35 or cx > w * 0.65:
                detections.append((cx, cy))
    cap.release()

    if len(detections) < 2:
        return None

    med_cx = float(np.median([d[0] for d in detections]))
    med_cy = float(np.median([d[1] for d in detections]))

    crop_w = int(w * 0.24)
    crop_h = int(h * 0.26)
    x0 = max(0, min(int(med_cx - crop_w * 0.50), w - crop_w))
    y0 = max(0, min(int(med_cy - crop_h * 0.42), h - crop_h))
    return f"crop={crop_w}:{crop_h}:{x0}:{y0}"


def render_split_clip(raw_video, start_sec, duration, output_path="final_clip.mp4"):
    trimmed = "trimmed.mp4"
    clip_wav = "clip_voice.wav"

    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(duration),
            "-i", raw_video, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", trimmed
        ],
        capture_output=True, check=True
    )

    cam_crop = detect_facecam_box(trimmed)
    if not cam_crop:
        print("No confirmed streamer facecam detected — skipping clip to avoid blank wall crops.")
        return None, None

    subprocess.run(
        ["ffmpeg", "-y", "-i", trimmed, "-ac", "1", "-ar", "16000", clip_wav],
        capture_output=True, check=True
    )
    ass_file, transcript = generate_word_subtitles(clip_wav)

    if not is_safe_text(transcript):
        print("Transcript triggered YouTube safety filter — skipping clip.")
        return None, None

    print(f"Facecam locked via OpenCV: {cam_crop}")
    filter_complex = (
        f"[0:v]{cam_crop},scale=1080:660:force_original_aspect_ratio=increase,crop=1080:660,eq=contrast=1.05:saturation=1.08[cam];"
        f"[0:v]crop=ih*(1080/1260):ih:(iw-ih*(1080/1260))/2:0,scale=1080:1260[game];"
        f"[cam][game]vstack=inputs=2,setpts=0.97*PTS,ass={ass_file}[v];"
        f"[0:a]highpass=f=180,lowpass=f=4500,atempo=1.03[a]"
    )

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", trimmed,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k",
            output_path
        ],
        capture_output=True, check=True
    )
    return output_path, transcript


def make_viral_caption(streamer, transcript, clip_title="", is_gta6=False):
    name = streamer["name"]
    platform = streamer.get("platform", "Twitch")
    game_tag = "#gta6 " if is_gta6 else ""
    credit_line = f"Credit: @{name} on {platform}"

    clean_clip_title = re.sub(r"[^\w\s'!?.,-]", "", clip_title).strip()
    if clean_clip_title and len(clean_clip_title) > 4 and is_safe_text(clean_clip_title):
        fallback_hook = f"{clean_clip_title[:65]} ({name}) 💀 {game_tag}#gaming #{name.lower()} #shorts"
    else:
        fallback_hook = f"{name} couldn't believe what just happened 💀 {game_tag}#gaming #{name.lower()} #shorts"

    fallback = f"{fallback_hook}\n\n{credit_line}"

    if not GEMINI_API_KEY:
        return fallback

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"Write a unique, advertiser-friendly, viral 1-sentence YouTube Shorts title (under 75 chars) about this gaming clip of {name}. "
            f"Context: '{clean_clip_title}'. Dialogue: '{transcript[:280]}'. "
            f"Keep it completely safe for YouTube Community Guidelines (no sexual/explicit words or slurs). "
            f"End with {game_tag}#gaming #{name.lower()} #shorts. Return ONLY the single caption line."
        )
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        hook = res.text.strip().splitlines()[0][:125]
        if hook and is_safe_text(hook):
            print(f"Generated unique AI caption: {hook}")
            return f"{hook}\n\n{credit_line}"
    except Exception as e:
        print(f"Gemini caption warning ({e}); using clip-title fallback.")

    return fallback


def publish_to_all_platforms(public_video_url, caption):
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    org_res = requests.post(url, headers=headers, json={"query": "query { account { organizations { id } } }"}).json()
    if "errors" in org_res:
        raise RuntimeError(f"Buffer org query failed: {org_res}")
    org_id = org_res["data"]["account"]["organizations"][0]["id"]

    ch_query = """
    query GetChannels($orgId: OrganizationId!) {
      channels(input: { organizationId: $orgId }) { id name service isDisconnected }
    }
    """
    ch_res = requests.post(url, headers=headers, json={"query": ch_query, "variables": {"orgId": org_id}}).json()
    active_channels = [c for c in ch_res.get("data", {}).get("channels", []) if not c.get("isDisconnected")]
    print(f"Found {len(active_channels)} active Buffer channels: {[c['service'] for c in active_channels]}")

    if not active_channels:
        raise RuntimeError("No active/connected Buffer channels found!")

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id status } }
        ... on MutationError { message }
      }
    }
    """
    success_count = 0
    for ch in active_channels:
        service = ch["service"].lower()
        post_input = {
            "text": caption,
            "channelId": ch["id"],
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": [{"video": {"url": public_video_url}}]
        }
        if service == "instagram":
            post_input["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True}}
        elif service == "youtube":
            first_line = caption.splitlines()[0]
            yt_title = first_line.split("#")[0].strip()[:88] or "Crazy Stream Moment"
            post_input["metadata"] = {
                "youtube": {
                    "title": f"{yt_title} #shorts"[:100],
                    "categoryId": "20",
                    "privacy": "public",
                    "madeForKids": False,
                    "notifySubscribers": True
                }
            }

        res = requests.post(url, headers=headers, json={"query": mutation, "variables": {"input": post_input}}).json()
        print(f"Buffer response for {ch['service']} ({ch['name']}):", json.dumps(res))

        create_data = (res.get("data") or {}).get("createPost") or {}
        if "post" in create_data:
            success_count += 1
        else:
            print(f"WARNING: Failed to post to {ch['service']}: {res}")

    if success_count == 0:
        raise RuntimeError("Buffer rejected posts across all channels — check logs above.")


if __name__ == "__main__":
    history = load_history()
    candidates = collect_viral_candidates(STREAMERS, history)

    for candidate in candidates:
        raw_file = download_candidate(candidate)
        if raw_file == "skip_permanently":
            history["posted_ids"].append(candidate["id"])
            save_history(history)
            continue
        if not raw_file:
            continue

        streamer = candidate["streamer"]
        start_s, clip_dur = find_hype_window(raw_file, clip_len=38)
        final_clip, transcript = render_split_clip(raw_file, start_s, clip_dur)
        if not final_clip:
            history["posted_ids"].append(candidate["id"])
            save_history(history)
            continue

        caption = make_viral_caption(
            streamer,
            transcript,
            clip_title=candidate.get("title", ""),
            is_gta6=candidate["is_gta6"]
        )

        upload = cloudinary.uploader.upload(final_clip, resource_type="video")
        try:
            publish_to_all_platforms(upload["secure_url"], caption)
            history["posted_ids"].append(candidate["id"])
            history["last_streamer"] = streamer["name"]
            history["last_platform"] = streamer["platform"]
            save_history(history)
            time.sleep(90)
        finally:
            cloudinary.uploader.destroy(upload["public_id"], resource_type="video")
        break

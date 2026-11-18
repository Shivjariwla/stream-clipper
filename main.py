import json
import os
import random
import re
import subprocess
import time
import wave
import numpy as np
import requests
import cloudinary
import cloudinary.uploader
from faster_whisper import WhisperModel
from google import genai

CLOUDINARY_URL = os.environ["CLOUDINARY_URL"]
BUFFER_ACCESS_TOKEN = os.environ["BUFFER_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
HISTORY_FILE = "posted_history.json"

cloudinary.config(cloudinary_url=CLOUDINARY_URL)

# Add any Twitch or YouTube stream/video tabs you want to auto-clip
# cam_pos options: "top-left", "top-right", "bottom-left", "bottom-right"
STREAMERS = [
    {"name": "KaiCenat", "url": "https://www.twitch.tv/kaicenat/videos?filter=archives&sort=time", "cam_pos": "top-left"},
    {"name": "xQc", "url": "https://www.twitch.tv/xqc/videos?filter=archives&sort=time", "cam_pos": "top-left"},
    {"name": "CaseOh", "url": "https://www.twitch.tv/caseoh_/videos?filter=archives&sort=time", "cam_pos": "top-left"},
    {"name": "IShowSpeed", "url": "https://www.youtube.com/@IShowSpeed/streams", "cam_pos": "top-left"},
]


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"posted_ids": []}


def save_history(history):
    history["posted_ids"] = history["posted_ids"][-200:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def fetch_stream_chunk(streamer, raw_output="raw_stream.mp4"):
    """
    Resolves the single newest stream/VOD URL first (--playlist-items 1),
    then downloads only a 3-minute slice so it never hangs on full channel playlists.
    """
    if os.path.exists(raw_output):
        os.remove(raw_output)

    url = streamer["url"]
    print(f"Checking {streamer['name']} ({url})...")

    try:
        probe = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-items", "1", url],
            capture_output=True, text=True, timeout=45
        )
    except subprocess.TimeoutExpired:
        print(f"Probe timed out for {streamer['name']}, skipping...")
        return None, None

    target_url = None
    video_id = None

    if probe.returncode == 0 and probe.stdout.strip():
        try:
            info = json.loads(probe.stdout)
            entries = info.get("entries") or [info]
            for entry in entries:
                if entry:
                    target_url = entry.get("webpage_url") or entry.get("url")
                    video_id = entry.get("id")
                    if target_url:
                        break
        except Exception as e:
            print(f"JSON parse warning for {streamer['name']}: {e}")

    if not target_url:
        print(f"No stream/VOD URL found for {streamer['name']}, skipping...")
        return None, None

    if not video_id or video_id.lower() == streamer["name"].lower():
            video_id = f"{streamer['name']}_{int(time.time() // 10800)}"

    print(f"Found latest stream/VOD for {streamer['name']}: {target_url} (ID: {video_id})")

    # Try a 3-minute highlight window 20 minutes into the VOD/stream
    for section in ["*00:20:00-00:23:00", "*00:01:00-00:04:00"]:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--playlist-items", "1",
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "--merge-output-format", "mp4",
            "--download-sections", section,
            "--force-keyframes-at-cuts",
            "-o", raw_output,
            target_url
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if os.path.exists(raw_output) and os.path.getsize(raw_output) > 300_000:
                return raw_output, video_id
            print(f"Section {section} returned no file: {res.stderr[-250:]}")
        except subprocess.TimeoutExpired:
            print(f"Section {section} timed out for {streamer['name']}")
            if os.path.exists(raw_output):
                os.remove(raw_output)

    return None, None


def find_hype_window(video_path, clip_len=38):
    """Finds the loudest reaction/screaming spike in the 3-minute capture."""
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
    if total_sec <= clip_len + 2:
        return 0.0, min(clip_len, total_sec)

    sec_energies = []
    for s in range(int(total_sec)):
        chunk = samples[s * rate : (s + 1) * rate]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) > 0 else 0.0
        sec_energies.append(rms)

    peak_sec = int(np.argmax(sec_energies))
    start_sec = max(0.0, min(float(peak_sec - 6), total_sec - clip_len))
    return start_sec, clip_len


def generate_word_subtitles(clip_audio_path, ass_path="subs.ass"):
    """Transcribes clip audio with Faster-Whisper word timestamps into styled ASS subtitles."""
    model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_audio_path, word_timestamps=True)

    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,68,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append((w.word.strip(), w.start, w.end))

    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    lines = [ass_header]
    transcript_parts = []
    chunk_size = 3
    for i in range(0, len(words), chunk_size):
        group = words[i : i + chunk_size]
        phrase = " ".join(w[0] for w in group).upper()
        phrase = re.sub(r"[^\w\s'!?.,-]", "", phrase)
        if not phrase:
            continue
        start_t = fmt_time(group[0][1])
        end_t = fmt_time(max(group[-1][2], group[0][1] + 0.25))
        lines.append(f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{phrase}")
        transcript_parts.append(phrase)

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return ass_path, " ".join(transcript_parts)


def render_split_clip(raw_video, start_sec, duration, cam_pos, output_path="final_clip.mp4"):
    """
    Builds a 1080x1920 vertical split-screen video:
    - Top (1080x660): Zoomed Facecam
    - Bottom (1080x1260): Center-cropped Gameplay
    - Center Seam: Burned-in animated subtitles
    """
    trimmed = "trimmed.mp4"
    clip_wav = "clip_voice.wav"

    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(start_sec), "-t", str(duration),
            "-i", raw_video, "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", trimmed
        ],
        capture_output=True, check=True
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", trimmed, "-ac", "1", "-ar", "16000", clip_wav],
        capture_output=True, check=True
    )

    ass_file, transcript = generate_word_subtitles(clip_wav)

    cam_crops = {
        "top-left": "crop=iw*0.26:ih*0.30:0:0",
        "top-right": "crop=iw*0.26:ih*0.30:iw*0.74:0",
        "bottom-left": "crop=iw*0.26:ih*0.30:0:ih*0.70",
        "bottom-right": "crop=iw*0.26:ih*0.30:iw*0.74:ih*0.70",
    }
    cam_crop = cam_crops.get(cam_pos, cam_crops["top-left"])

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
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            output_path
        ],
        capture_output=True, check=True
    )
    return output_path, transcript


def make_viral_caption(streamer_name, transcript):
    fallback = f"{streamer_name}'s reaction is unreal 💀🔥 #gaming #{streamer_name.lower()} #twitch #shorts"
    if not GEMINI_API_KEY or not transcript.strip():
        return fallback
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"Write a short, viral 1-sentence YouTube Shorts / TikTok caption (under 90 chars) for a gaming clip of {streamer_name}. "
            f"Transcript snippet: '{transcript[:250]}'. "
            f"Include #gaming #shorts and #{streamer_name.lower()}. Return ONLY the caption text."
        )
        res = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)
        return res.text.strip()[:140] or fallback
    except Exception:
        return fallback


def publish_to_all_platforms(public_video_url, caption):
    url = "https://api.buffer.com"
    headers = {
        "Authorization": f"Bearer {BUFFER_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    org_res = requests.post(url, headers=headers, json={"query": "query { account { organizations { id } } }"}).json()
    org_id = org_res["data"]["account"]["organizations"][0]["id"]

    ch_query = """
    query GetChannels($orgId: OrganizationId!) {
      channels(input: { organizationId: $orgId }) { id name service isDisconnected }
    }
    """
    ch_res = requests.post(url, headers=headers, json={"query": ch_query, "variables": {"orgId": org_id}}).json()
    active_channels = [c for c in ch_res.get("data", {}).get("channels", []) if not c.get("isDisconnected")]

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id status } }
        ... on MutationError { message }
      }
    }
    """
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
            yt_title = caption.split("#")[0].strip()[:88] or "Insane Stream Reaction"
            post_input["metadata"] = {"youtube": {"title": f"{yt_title} #shorts"[:100], "categoryId": "20"}}

        res = requests.post(url, headers=headers, json={"query": mutation, "variables": {"input": post_input}}).json()
        print(f"Published clip to {ch['service']} ({ch['name']}):", res)


if __name__ == "__main__":
    history = load_history()
    random.shuffle(STREAMERS)

    for streamer in STREAMERS:
        raw_file, vid_id = fetch_stream_chunk(streamer)
        if not raw_file or vid_id in history["posted_ids"]:
            continue

        start_s, clip_dur = find_hype_window(raw_file, clip_len=38)
        final_clip, transcript = render_split_clip(raw_file, start_s, clip_dur, streamer["cam_pos"])
        caption = make_viral_caption(streamer["name"], transcript)

        upload = cloudinary.uploader.upload(final_clip, resource_type="video")
        try:
            publish_to_all_platforms(upload["secure_url"], caption)
            history["posted_ids"].append(vid_id)
            save_history(history)
            time.sleep(180)
        finally:
            cloudinary.uploader.destroy(upload["public_id"], resource_type="video")
        break

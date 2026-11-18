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

# Twitch top 24h clips download in ~2 seconds and already capture viral stream moments!
# cam_pos options: "top-left", "mid-left", "top-right", "bottom-left", "bottom-right"
STREAMERS = [
    {"name": "KaiCenat", "url": "https://www.twitch.tv/kaicenat/clips?filter=clips&range=24hr", "cam_pos": "top-left"},
    {"name": "xQc", "url": "https://www.twitch.tv/xqc/clips?filter=clips&range=24hr", "cam_pos": "top-left"},
    {"name": "CaseOh", "url": "https://www.twitch.tv/caseoh_/clips?filter=clips&range=24hr", "cam_pos": "mid-left"},
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


def fetch_stream_chunk(streamer, history, raw_output="raw_stream.mp4"):
    """
    Fetches the top unposted 24h Twitch clip (instant ~2s download) or
    a 2-minute highlight slice from a completed YouTube stream (skipping upcoming premieres).
    """
    if os.path.exists(raw_output):
        os.remove(raw_output)

    url = streamer["url"]
    print(f"Checking {streamer['name']} ({url})...")

    try:
        probe = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", "5", url],
            capture_output=True, text=True, timeout=45
        )
    except subprocess.TimeoutExpired:
        print(f"Probe timed out for {streamer['name']}, skipping...")
        return None, None

    candidates = []
    if probe.returncode == 0 and probe.stdout.strip():
        try:
            info = json.loads(probe.stdout)
            entries = info.get("entries") or [info]
            for entry in entries:
                if not entry:
                    continue
                if entry.get("live_status") == "is_upcoming":
                    continue
                t_url = entry.get("webpage_url") or entry.get("url")
                v_id = entry.get("id")
                if t_url and v_id and v_id not in history["posted_ids"]:
                    candidates.append((t_url, v_id))
        except Exception as e:
            print(f"JSON parse warning for {streamer['name']}: {e}")

    if not candidates:
        print(f"No new unposted clips/streams found for {streamer['name']}, skipping...")
        return None, None

    target_url, video_id = candidates[0]
    print(f"Selected for {streamer['name']}: {target_url} (ID: {video_id})")

    is_twitch_clip = "twitch.tv" in target_url and "/clip/" in target_url

    if is_twitch_clip:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", raw_output,
            target_url
        ]
    else:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f", "bv*[height<=720]+ba/b[height<=720]/best",
            "--merge-output-format", "mp4",
            "--download-sections", "*00:15:00-00:17:00",
            "-o", raw_output,
            target_url
        ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if os.path.exists(raw_output) and os.path.getsize(raw_output) > 250_000:
            return raw_output, video_id
        print(f"Download failed for {streamer['name']}: {res.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        print(f"Download timed out for {streamer['name']}")
        if os.path.exists(raw_output):
            os.remove(raw_output)

    return None, None


def find_hype_window(video_path, clip_len=38):
    """Finds the loudest reaction/screaming spike (or uses the full clip if <= 42s)."""
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
    """Transcribes clip audio with Faster-Whisper word timestamps into styled ASS subtitles."""
    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(clip_audio_path, word_timestamps=True)

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
        end_sec = min(group[-1][2] + 0.25, group[0][1] + 2.2)
        end_t = fmt_time(max(end_sec, group[0][1] + 0.3))
        lines.append(f"Dialogue: 0,{start_t},{end_t},Default,,0,0,0,,{phrase}")
        transcript_parts.append(phrase)

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return ass_path, " ".join(transcript_parts)


def render_split_clip(raw_video, start_sec, duration, cam_pos, output_path="final_clip.mp4"):
    """Builds a 1080x1920 vertical split-screen video with facecam top, game bottom, and subtitles."""
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
        "mid-left": "crop=iw*0.26:ih*0.30:0:ih*0.18",
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
        raw_file, vid_id = fetch_stream_chunk(streamer, history)
        if not raw_file:
            continue

        start_s, clip_dur = find_hype_window(raw_file, clip_len=38)
        final_clip, transcript = render_split_clip(raw_file, start_s, clip_dur, streamer["cam_pos"])
        caption = make_viral_caption(streamer["name"], transcript)

        upload = cloudinary.uploader.upload(final_clip, resource_type="video")
        try:
            publish_to_all_platforms(upload["secure_url"], caption)
            history["posted_ids"].append(vid_id)
            save_history(history)
            time.sleep(90)
        finally:
            cloudinary.uploader.destroy(upload["public_id"], resource_type="video")
        break

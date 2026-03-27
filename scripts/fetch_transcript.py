"""
fetch_transcript.py

Fetches the transcript from a YouTube video using youtube-transcript-api.
Handles multilingual videos (tries all available languages if English is not found).
Saves the transcript as a .txt file in the transcripts/ directory.

Usage:
    python scripts/fetch_transcript.py <youtube_url>

Requirements:
    pip install youtube-transcript-api
"""

import sys
import os
import re

def extract_video_id(url):
    match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if not match:
        raise ValueError(f"Could not extract video ID from URL: {url}")
    return match.group(1)

def fetch_transcript(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()

    # List available transcripts
    transcript_list = api.list(video_id)
    available = [(t.language_code, t.is_generated) for t in transcript_list]
    print(f"Available transcripts: {available}")

    # Try English first, then fall back to whatever is available
    try:
        transcript = api.fetch(video_id, languages=['en'])
        language = 'en'
    except Exception:
        # Fall back to first available language
        first_lang = available[0][0]
        print(f"English not available, fetching '{first_lang}'")
        transcript = api.fetch(video_id, languages=[first_lang])
        language = first_lang

    text = ' '.join([t.text for t in transcript])
    return text, language

def save_transcript(video_id, text, output_dir="transcripts"):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{video_id}.txt")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print(f"Transcript saved to: {path}")
    return path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/fetch_transcript.py <youtube_url>")
        sys.exit(1)

    url = sys.argv[1]
    video_id = extract_video_id(url)
    print(f"Video ID: {video_id}")

    text, language = fetch_transcript(video_id)
    print(f"Language: {language} | Length: {len(text)} characters")

    path = save_transcript(video_id, text)
    print(f"\n--- Transcript preview (first 500 chars) ---\n{text[:500]}")

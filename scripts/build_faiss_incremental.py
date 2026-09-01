#!/usr/bin/env python3
"""
Incremental FAISS index builder for GD RAG pipeline.
Processes comments, setlists, and deadcast transcripts in batches,
adding to the index incrementally to avoid OOM.
"""
import sys
# Ensure user-local packages are available
if '/home/hermes/.local/lib/python3.12/site-packages' not in sys.path:
    sys.path.insert(0, '/home/hermes/.local/lib/python3.12/site-packages')

import gc
import json
import os
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

# Add index dir to path
sys.path.insert(0, "/home/mao/DaveMatt/gd-project/index")
from build_index import normalize_setlist

DATA_PATH = "/home/mao/DaveMatt/gd-project/data/gd_comments_combined.json"
INDEX_DIR = "/home/mao/DaveMatt/gd-project/index"
BATCH_SIZE = 64  # Increased from 16 - we now have 3.8 GB RAM


def main():
    print("Loading model...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    dim = model.get_embedding_dimension()
    print(f"Model loaded, dim={dim}")

    print(f"Loading data from {DATA_PATH}...")
    with open(DATA_PATH) as f:
        data = json.load(f)

    comments = data.get("comments", [])
    setlists = data.get("setlists", {})
    deadcast_transcripts = data.get("deadcast_transcripts", [])
    print(f"  {len(comments)} comments, {len(setlists)} setlists, {len(deadcast_transcripts)} deadcast transcripts")

    index = faiss.IndexFlatL2(dim)
    metadata = []

    # --- Process setlists ---
    setlist_texts = []
    setlist_meta = []
    for show_id, sl_raw in setlists.items():
        sl = normalize_setlist(show_id, sl_raw)
        if not sl or not sl.get("songs"):
            continue

        seen = set()
        for s_set in sl.get("sets", []):
            for se in s_set.get("songs", []):
                name = se.get("name", "").strip()
                if name and name not in seen:
                    seen.add(name)
                    setlist_texts.append(f"{name} - Grateful Dead setlist")
                    m = {
                        "idx": len(metadata) + len(setlist_meta),
                        "show_identifier": show_id,
                        "song_name": name,
                        "created": sl.get("date", ""),
                        "comment_text": f"[Setlist] {name} performed at {show_id}",
                        "type": "setlist_song",
                    }
                    if se.get("duration"):
                        m["duration"] = se["duration"]
                    setlist_meta.append(m)

        for s in sl.get("songs", []):
            name = s.get("name", "") if isinstance(s, dict) else str(s)
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                setlist_texts.append(f"{name} - Grateful Dead setlist")
                setlist_meta.append({
                    "idx": len(metadata) + len(setlist_meta),
                    "show_identifier": show_id,
                    "song_name": name,
                    "created": sl.get("date", ""),
                    "comment_text": f"[Setlist] {name} performed at {show_id}",
                    "type": "setlist_song",
                })

        for g in sl.get("guest_artists", []):
            if not g:
                continue
            setlist_texts.append(f"{g} played with Grateful Dead at {show_id}")
            setlist_meta.append({
                "idx": len(metadata) + len(setlist_meta),
                "show_identifier": show_id,
                "guest_artist": g,
                "created": sl.get("date", ""),
                "comment_text": f"[Guest] {g} performed with Grateful Dead",
                "type": "guest_artist",
            })

        for t in sl.get("transitions", []):
            fr, to = t.get("from", ""), t.get("to", "")
            if fr and to:
                setlist_texts.append(f"Transition: {fr} -> {to}")
                setlist_meta.append({
                    "idx": len(metadata) + len(setlist_meta),
                    "show_identifier": show_id,
                    "transition_from": fr,
                    "transition_to": to,
                    "created": sl.get("date", ""),
                    "comment_text": f"[Transition] {fr} -> {to}",
                    "type": "transition",
                })

    print(f"Setlist entries: {len(setlist_texts)}")
    print("Embedding setlists in batches...")
    for i in range(0, len(setlist_texts), BATCH_SIZE):
        batch = setlist_texts[i:i+BATCH_SIZE]
        emb = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
        index.add(emb)
        del emb, batch
        if (i // BATCH_SIZE) % 16 == 0:
            gc.collect()
            print(f"  Setlist progress: {i}/{len(setlist_texts)} done", flush=True)

    metadata.extend(setlist_meta)
    print(f"  Setlist vectors: {index.ntotal}")
    del setlist_texts, setlist_meta
    gc.collect()

# --- Process deadcast transcripts ---
    dc_texts = []
    dc_meta = []
    for dt in deadcast_transcripts:
        transcript = dt.get("transcript", "").strip()
        if not transcript:
            continue
        # Split long transcripts into chunks
        chunk_size = 500
        words = transcript.split()
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i:i+chunk_size])
            dc_texts.append(chunk)
            dc_meta.append({
                "idx": len(metadata) + len(dc_meta),
                "show_identifier": "",
                "source": "deadcast",
                "title": dt.get("title", ""),
                "url": dt.get("url", ""),
                "chunk_index": i // chunk_size,
                "word_count": len(chunk.split()),
                "comment_text": f"[Deadcast] {dt.get('title', '')} (chunk {i // chunk_size})",
                "type": "deadcast_transcript",
            })

    print(f"Deadcast chunks: {len(dc_texts)}")
    if dc_texts:
        print("Embedding deadcast transcripts...")
        for i in range(0, len(dc_texts), BATCH_SIZE):
            batch = dc_texts[i:i+BATCH_SIZE]
            emb = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
            index.add(emb)
            del emb, batch
            if i % 100 == 0:
                gc.collect()
                print(f"  Deadcast progress: {i}/{len(dc_texts)} done", flush=True)
        metadata.extend(dc_meta)
        print(f"  After deadcast: {index.ntotal} vectors")
    del dc_texts, dc_meta
    gc.collect()

    # --- Process EP durations ---
    ep_texts = []
    ep_meta = []
    ep_path = os.path.join(os.path.dirname(DATA_PATH), "ep_durations.json")
    if os.path.exists(ep_path):
        with open(ep_path) as f:
            ep_data = json.load(f)
        print(f"EP durations loaded: {len(ep_data)} entries")
        for entry in ep_data:
            song_name = entry.get("song", "Estimated Prophet")
            dur_sec = entry.get("duration_seconds", 0)
            dur_str = entry.get("duration_display", "")
            show_id = entry.get("show_id", "")
            ep_texts.append(f"{song_name} by Grateful Dead - {dur_str} at {show_id}")
            ep_meta.append({
                "idx": len(metadata) + len(ep_meta),
                "show_identifier": show_id,
                "song_name": song_name,
                "duration": dur_str,
                "duration_seconds": dur_sec,
                "track_file": entry.get("track_file", ""),
                "source": entry.get("source", "positional"),
                "comment_text": f"[EP Duration] {song_name}: {dur_str} ({dur_sec}s) at {show_id}",
                "type": "ep_duration",
            })

        if ep_texts:
            print("Embedding EP durations...")
            for i in range(0, len(ep_texts), BATCH_SIZE):
                batch = ep_texts[i:i+BATCH_SIZE]
                emb = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
                index.add(emb)
                del emb, batch
                if (i // BATCH_SIZE) % 4 == 0:
                    gc.collect()
                    print(f"  EP progress: {i}/{len(ep_texts)} done", flush=True)
            metadata.extend(ep_meta)
            print(f"  After EP: {index.ntotal} vectors")
    del ep_texts, ep_meta
    gc.collect()

    # --- Process comments ---
    comment_texts = []
    comment_meta = []
    for c in comments:
        text = c.get("comment_text", "").strip()
        if not text:
            continue
        comment_texts.append(text)
        comment_meta.append({
            "idx": len(metadata) + len(comment_meta),
            "show_identifier": c.get("show_identifier", ""),
            "reviewer": c.get("reviewer", "") or "anonymous",
            "rating": c.get("rating", ""),
            "created": c.get("created", ""),
            "comment_text": text,
            "type": "comment",
        })

    print(f"Comments to embed: {len(comment_texts)}")
    print("Embedding comments in batches...")
    for i in range(0, len(comment_texts), BATCH_SIZE):
        batch = comment_texts[i:i+BATCH_SIZE]
        emb = model.encode(batch, show_progress_bar=False, convert_to_numpy=True)
        index.add(emb)
        del emb, batch
        if (i // BATCH_SIZE) % 8 == 0:
            gc.collect()
            print(f"  Comment progress: {i}/{len(comment_texts)} done", flush=True)

    del comment_texts
    gc.collect()
    metadata.extend(comment_meta)
    print(f"\nTotal vectors: {index.ntotal}")

    # --- Save ---
    os.makedirs(INDEX_DIR, exist_ok=True)
    faiss_path = os.path.join(INDEX_DIR, "vector_index.faiss")
    meta_path = os.path.join(INDEX_DIR, "index_metadata.json")

    faiss.write_index(index, faiss_path)
    ep_path = os.path.join(os.path.dirname(DATA_PATH), "ep_durations.json")
    ep_data = []
    if os.path.exists(ep_path):
        with open(ep_path) as f:
            ep_data = json.load(f)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "comments": metadata,
            "setlists": setlists,
            "deadcast_transcripts": deadcast_transcripts,
            "ep_durations": ep_data
        }, f, indent=2, ensure_ascii=False)

    print(f"Saved: {faiss_path}")
    print(f"Saved: {meta_path}")

    tc = {}
    for m in metadata:
        t = m.get("type", "unknown")
        tc[t] = tc.get(t, 0) + 1
    print(f"Breakdown: {tc}")
    print("Done!")


if __name__ == "__main__":
    main()

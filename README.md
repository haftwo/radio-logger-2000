# Radio Logger - 2000

Records internet radio streams to disk in clock-aligned chunks and gives you a web dashboard to replay any moment, see the waveform, listen live, and (for music stations) see what played with artwork.

## What's in the box

- **Dashboard** – pick a station (with its logo) and a date, click anywhere on the 24-hour timeline to start playback, scrub the waveform, step chunk to chunk (playback rolls on to the next chunk and the next day automatically), download a chunk, or hit **Listen live**. Keyboard: space = play/pause, ←/→ = 10 s.
- **Music stations** – show the cover art and track for whatever moment you're replaying, plus a "Played on this day" list you can click to jump to a song. Track changes are marked on the timeline and waveform.
- **Admin** (admin role only)
  - *Streams*: add/edit/delete, enable/disable, restart a recorder, upload a station logo (click the logo), see the recorder log.
  - *Recording*: recordings folder, how long to keep audio, chunk size (1–60 min), MP3 bitrate, Shazam interval, and disk usage.
  - *Users*: add/edit/delete users, reset passwords, Admin vs Viewer roles.

## How it works

| Piece | Detail |
|---|---|
| Recorder | One `ffmpeg` per enabled stream, re-encoded to CBR MP3 and split with the segment muxer on clock boundaries. Auto-restarts with back-off if the stream drops. |
| Files | `<recordings>/<station-slug>/YYYY/MM/DD/YYYYMMDD-HHMMSS.mp3` in the container's `TZ`. Waveform data is cached next to each chunk as `.peaks.json`. |
| Retention | Hourly sweep deletes chunks and track history older than the retention setting. |
| Live | `/api/streams/{id}/live` re-encodes the source to MP3, so HLS/AAC/http streams all play in the browser, including over your https Cloudflare hostname. |
| Track detection | **Stream metadata** reads Icecast/Shoutcast `StreamTitle`. **Shazam** fingerprints the last ~12 s of the current chunk every N seconds (for HLS streams like Double J that have no ICY tags; uses the unofficial `shazamio` library). Artwork comes from Shazam, otherwise the iTunes Search API. |
| Auth | Session cookie, PBKDF2-hashed passwords in SQLite at `/config/radiologger.db`. |

## Install on Unraid

The image is built by GitHub Actions on every push to `main` and published to
`ghcr.io/haftwo/radio-logger-2000:latest` (amd64 and arm64).

1. Copy `unraid/radio-logger-2000.xml` to `/boot/config/plugins/dockerMan/templates-user/my-radio-logger-2000.xml`
   (or download it straight there:
   `wget -O /boot/config/plugins/dockerMan/templates-user/my-radio-logger-2000.xml https://raw.githubusercontent.com/haftwo/radio-logger-2000/main/unraid/radio-logger-2000.xml`).
2. *Docker → Add Container → Template* → pick **radio-logger-2000**, check the paths, Apply.
   Or with the Compose Manager plugin: `docker compose up -d` using `docker-compose.yml`.
3. Open `http://<unraid-ip>:8765`, sign in as `admin` / `changeme` (or whatever you set in `RL_ADMIN_USER` / `RL_ADMIN_PASSWORD`) and **change the password** under Admin → Users.
4. Add your streams in Admin → Streams.

### Volumes and settings

| Container path | Suggested host path | Holds |
|---|---|---|
| `/config` | `/mnt/user/appdata/radio-logger-2000` | database, secret key, station logos |
| `/recordings` | `/mnt/user/radio-logs` | the audio |

Set `TZ` to your zone so file names and the timeline match local time. If you change the recordings folder in Admin, make sure the new path is inside a mapped volume (e.g. map a second share to `/recordings2` and point the setting there). Existing recordings aren't moved.

Storage guide: 128 kbps ≈ 1.4 GB per station per day.

### Publishing through your Cloudflare Tunnel

Point a public hostname (e.g. `radiologger.example.com`) at `http://radio-logger-2000:8000` (if cloudflared shares a Docker network with it) or `http://<unraid-ip>:8765`. Run only **one** uvicorn worker — the recorders live inside the app process.

## Updating

Push to `main`, wait for the **Build and publish Docker image** action to finish, then in Unraid use
*Docker → Check for Updates → apply update* (or `docker compose pull && docker compose up -d`).
To build locally instead: `docker build -t radio-logger-2000:latest .`
Your database, logos and audio live in the volumes, so rebuilding is safe.

## Local development

```bash
pip install -r requirements.txt
RL_CONFIG_DIR=./data/config RL_RECORDINGS_DIR=./data/rec uvicorn app.main:app --reload
```

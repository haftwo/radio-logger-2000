# Radio Logger - 2000

Records internet radio streams to disk in clock-aligned chunks and gives you a web dashboard to replay any moment, see the waveform, listen live, and (for music stations) see what played with artwork.

## What's in the box

- **Dashboard** – pick a station (with its logo) and a date, click anywhere on the 24-hour timeline to start playback, scrub the waveform, step chunk to chunk (playback rolls on to the next chunk and the next day automatically), download a chunk, or hit **Listen live**. Jump straight to a moment with the date + time box and **Go**, or deep-link with `/?stream=1&date=2026-11-01&time=08:30:00`. Zoom the 24-hour timeline down to 5 minutes (wheel, +/−, **Day** to reset; drag to pan) and the waveform, the +/− buttons or keys (0 = fit), drag to pan, click to seek. Keyboard: space = play/pause, ←/→ = 10 s.
- **Song tags** – every detected song is tagged automatically with artist, title, album, genre and year (from Shazam and the iTunes catalogue) and shown as a coloured block on the timeline and a labelled band on the waveform. Fix a wrong match or add a missed song with **Edit** / **+ Tag song at playhead**; corrected songs get fresh artwork and tags.
- **Station details** – the station name, genre and description the stream announces (ICY headers, or the HLS/TS service name) are picked up automatically; leave the name blank when adding a stream, or use **Detect from stream**.
- **Music stations** – show the cover art and track for whatever moment you're replaying, plus a "Played on this day" list you can click to jump to a song. Track changes are marked on the timeline and waveform.
  - *Top played* tab: the top 10 songs for the selected day and for the 7 days ending on it, with play counts. Click a song to jump to the last time it played.
  - *Export*: download played songs as CSV (date, time, station, artist, title, album, tags, duration, source) for the day, the 7 days to that day, or everything logged.
- **Outages** – live health indicator for every stream (Healthy / Quiet / Silent / No signal) with 24-hour and 7-day availability, plus a log of every outage: any silence or loss of audio lasting more than 30 s (configurable). Filter by station and period, download as CSV, and jump straight to the moment it dropped with **Replay**.
- **Admin** (admin role only)
  - *Streams*: add/edit/delete, enable/disable, restart a recorder, upload a station logo (click the logo), see the recorder log.
  - *Recording*: recordings folder, how long to keep audio, chunk size (1–60 min), MP3 bitrate, Shazam interval, and disk usage.
  - *Users*: add/edit/delete users, reset passwords, Admin vs Viewer roles.
  - *Alarms*: outage threshold and silence level, and email alarms over SMTP (who to email, how long before alerting, recovery emails, test email).

## How it works

| Piece | Detail |
|---|---|
| Recorder | One `ffmpeg` per enabled stream, re-encoded to CBR MP3 and split with the segment muxer on clock boundaries. Auto-restarts with back-off if the stream drops. |
| Files | `<recordings>/<station-slug>/YYYY/MM/DD/YYYYMMDD-HHMMSS.mp3` in the container's `TZ`. Waveform data is cached next to each chunk as `.peaks.json`. |
| Retention | Hourly sweep deletes chunks and track history older than the retention setting. |
| Live | `/api/streams/{id}/live` re-encodes the source to MP3, so HLS/AAC/http streams all play in the browser, including over your https Cloudflare hostname. |
| Track detection | **Automatic** (default) reads Icecast/Shoutcast `StreamTitle` and falls back to Shazam when the stream has no titles (HLS, or titles that can't be read). Detection status shows on Admin → Streams. **Shazam** fingerprints the last ~12 s of the current chunk every N seconds (for HLS streams like Double J that have no ICY tags; uses the unofficial `shazamio` library). Artwork comes from Shazam, otherwise the iTunes Search API. |
| Outage detection | Each recorder runs ffmpeg's `silencedetect` filter and reports encoder progress. Silence below the set level (default −50 dB), or no audio arriving at all, for longer than the threshold opens an outage; it closes once audio has been back for 5 s. Outages survive container restarts. Kept for a year. |
| Email alarms | Plain SMTP (STARTTLS, SSL or none). Sent once an outage has lasted the alarm delay, and again on recovery. Set the Public URL (Admin → Single sign-on) to get replay links in the emails. |
| Version | Every build from GitHub Actions is stamped `v1.<build number>` with its commit and build time. It shows next to the logo on every page (hover for detail) and in `/healthz`, so you can tell at a glance whether Unraid has pulled the latest image. Running from source shows "dev build". |
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

## Transcripts for talk stations (local Whisper)

Talk stations (streams not marked "Music station") show a **Transcript** panel instead of
"Played on this day". Admin → **Transcription** points the logger at a Whisper server on your
network:

| Server | Type | Address |
|---|---|---|
| Speaches / faster-whisper-server / LocalAI | OpenAI-compatible | `http://<unraid-ip>:8000` |
| openai-whisper-asr-webservice | whisper-asr-webservice | `http://<unraid-ip>:9000` |
| linuxserver/faster-whisper (Home Assistant) | Wyoming | `tcp://<unraid-ip>:10300` (15 s timing blocks) |

Finished chunks are transcribed newest first, then the backlog within the look-back window.
Lines are timestamped and searchable (full-text, prefix and "exact phrase", this day or all days).
Names, places and frequent topics are tagged automatically, plus a watch-list of your own keywords.
Clicking a line (or a search result on another day) moves the timeline and waveform there and plays.
Search hits are marked on the timeline. Transcripts follow the same retention as the audio.

## Single sign-on (Microsoft Entra ID / Azure AD)

Admin → **Single sign-on** adds a "Sign in with Microsoft" button using OpenID Connect
(authorization code + PKCE). The tab shows the redirect URI to register and walks through the
Entra app registration. In short:

1. Entra admin centre → App registrations → New registration (single tenant), redirect URI
   `https://<your-hostname>/auth/callback` (Web). HTTPS is required, and the Cloudflare hostname works.
2. Paste the tenant ID, client ID and a client secret **Value** into the tab and tick Enable.
3. Create app roles with values `Admin` and `Viewer`, and assign users/groups in Enterprise applications.
   Or use a groups claim and paste the admin group's object ID.

Users are created on first sign-in and their role is re-synced from Entra on every sign-in.
"Password sign-in → Local admin accounts only" turns everything else over to SSO while keeping a
local admin as break-glass. The app won't let you remove the last one. Settings can be pre-seeded
with `RL_SSO_TENANT_ID`, `RL_SSO_CLIENT_ID`, `RL_SSO_CLIENT_SECRET`, `RL_SSO_ADMIN_GROUP`,
`RL_PUBLIC_URL` and `RL_SSO_ENABLED=true`.

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

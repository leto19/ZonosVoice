# ZonosVoice: notes for developers

This file is not in the release (`make_release.py` uses an allowlist). `README.md` is the player guide.

## Release

    python3 make_release.py        # dist/ZonosVoice-<version>.zip

The script zips an allowlist of source files. It stops if a listed file is missing, if an audio file would go into
the zip, or if a file holds a local path, an internal host name (`vp-dgx`, `sgl_rawclone`, `gradio.live`) or an API
key. Voice clips are never in a release: each player makes them from their own game (`extract-game-voices`).

There is no LICENSE file yet. The script warns about that.

## Companion launcher

`zonosvoice.py launcher` (and the menu shortcut that `shortcut` writes) runs the daemon in a thread and opens the
real `openmw-launcher`. Facts from the 0.51 launcher source (`apps/launcher/maindialog.cpp`, `MainDialog::play`):
the launcher starts `openmw` detached and then quits. The companion therefore waits for an OpenMW game process to
appear (up to 20 s after the launcher closes), then for it to exit, and then ends with `os._exit` (it does not wait
for open TTS requests). Game process names: `openmw` / `openmw.x86_64` (Linux, from /proc/*/comm), `openmw.exe`
(Windows, tasklist), `openmw` (macOS, pgrep).

The launcher keeps content lists as profiles in `launcher.cfg`. When `openmw.cfg` changed outside the launcher, it
makes a new profile named with a timestamp. Therefore the companion adds ZonosVoice to `openmw.cfg` before it opens
the launcher, and the launcher shows it as ticked. The launcher keeps other `openmw.cfg` lines (for example
`skip-menu`) when it writes the file.

The release zip holds the 48 silent slot files and `zonosvoice/ready.txt` (`make_release.py` makes them), so a
player does not need `install`.

`./test_launcher.py` tests this with the real launcher in Xephyr. It uses a copy of your OpenMW config
(`XDG_CONFIG_HOME` points to `test_out_launcher/xdg`), removes the ZonosVoice lines from that copy, moves the
launcher window into the Xephyr screen, clicks "Launch OpenMW" through XTEST, and checks the parts and the exit.
Result (2026-10-15, zyphra): the companion added ZonosVoice to `openmw.cfg`; the game started at once after the
click and ran 126 s; the companion exited 1.0 s after the game; 3 lines, 6 parts made, 6 found in the game sound.

Shortcut check (Linux): the `.desktop` file passes `desktop-file-validate`, and its command runs from any folder.
The Windows (`.lnk` through PowerShell, `uvw` for no console window) and macOS (`.command` file) shortcuts are not
tested.

## One voice program only

Two daemons that follow the same `openmw.log` both speak every line (overlapping voices) and overwrite each other's
slot files. It happened on 2026-10-19: the cloud shortcut and the local shortcut were both started for one game.
`claim_daemon()` takes an exclusive lock on `<cache>/daemon.lock` (flock; msvcrt on Windows) for the life of the
process and writes the holder to `<cache>/daemon.info`. `run` exits with "ZonosVoice already runs (...)", `play`
checks before it starts the game, and `launcher` shows the message in a window and opens the launcher without a
daemon. Checked with two processes on a temporary cache folder.

## Cache: generic lines

The cache key is (backend tag, clip hash, text), so a line is made once per clip. Counts from the three masters
(2026-10-15): 23,418 spoken lines; 16,692 have a speaker filter (ONAM); 6,726 are generic. Of the generic lines,
6,039 have no `%Name` or `%PCName` and can be shared (5,518 of them have no race or sex filter either).

The three manner clips of a folder are one actor (ECAPA: Dark Elf male warm and curt score 0.79 against neutral;
Dark Elf female scores 0.17). Therefore generic lines use one shared clip per race and sex (`neutral`), and lines
with a speaker filter keep the NPC's own clip. The Lua script sends the scope (`speaker`, `generic`, `book`) as
field 10 of the `req` line; the daemon still reads the older 10-field format.

Pre-making lines instead was rejected: with one clip per race and sex, the shareable generic greetings alone are
17,100 clips (about 28 h of audio), the 8 common topics 23,700 clips (114 h), and all shareable generic lines 112,000
clips (370 h).

In-game check (2026-10-15, zyphra): three Dark Elf males used `darkelf_m/neutral.wav (shared)` for generic lines.
Meril Hlaano's "What do you want?" was made once (1.8 s), and Alvis Teri's identical line came from the cache
(0.0 s). Session summary: 5 lines, 7 parts, 2 from the cache, 5 API requests; all 7 parts found in the game sound.

Each session summary is added to `<cache>/stats.json` (`zonosvoice.py stats`). The tests set `ZONOSVOICE_STATS`
to a file in their output folder, so test runs do not count.

## Backends: the released ZONOS3 Mini only

Since version 0.4.0 (2026-11-10) the mod uses only the released ZONOS3 Mini, in two ways:

| Backend | Use | Wait for a new line (2026-11-10) |
|---|---|---|
| `zyphra` | Zyphra Cloud `/audio/speech`, model `zyphra/ZONOS3-Mini` (`MODEL`; on the API since 2026-11-09, $10 per 1M input bytes). The default for players. | about 2 to 3 s |
| `cpp` | zonos3.cpp (`zonos3-server`) on this computer, started by the voice program. | about 4 to 7 s for a short first part on the laptop CPU |

Version 0.3.0 also had the `minisgl`, `vp` (SSH tunnel to a slurm serving job) and `local` (the zonos-cpu torch
port) backends and a `[tts] model` setting; that script is in `dist/ZonosVoice-0.3.0.zip`. `load_settings` warns
about `[tts] model` and `url` in an old `config.toml` and ignores them.

### zonos3.cpp (`cpp`)

`[cpp] dir` is a built zonos3.cpp checkout (here `~/repos/zonos3.cpp`, backbone `zonos3-q8_0.gguf` because the
QAT GGUF is on the private HF repo; see the zonos3.cpp README). `Zonos3Cpp.start()` reuses a server at `[cpp] url`
or on `[cpp] port` (default 18871), or starts `<dir>/build/zonos3-server <model> --host 127.0.0.1 --port <port>
--cpu|--gpu --dac-threads <n>` (log `<cache>/zonos3_server.log`; ends with the voice program through
PR_SET_PDEATHSIG). The server answers `/health` after about 9 s, but then warms up the backbone and four decoder
sizes (20-30 s on the laptop CPU; the four `dac: ... decode 5-7 s` lines in the log), and the first request waits
behind that. `start()` therefore sends one small request without a speaker and reports ready when it returns.

- Protocol: `POST /tts/speakers` (header `X-TTS-Session-ID`, `reference_audio_base64` + `reference_audio_name` +
  `label`) returns `id` (`ref_0000000001`, per session, gone after a restart: `synth()` uploads again on "unknown
  reference_id"). The speaker embedding is made on the voice's first use (the speaker encoder loads then, about 5 s
  once); `prepare()` forces it with `GET /tts/speakers/<id>/embedding`, so the prewarm does it before the game.
  `POST /tts/generate` with `reference_id`, `speaker_mode` (`[cpp] clone`: `embedding`, or `reference` which needs
  `dac-encoder.gguf` and `ssl-teacher.gguf`), `seed`, `stream: false`, `format: "pcm"` (float32 44.1 kHz). Fields
  that start with `speaker_` (other than `speaker_mode`, `speaker_cfg_scale`, `speaker_embedding_*`), `spk*` or
  `emotion*` are HTTP 400; the old minisgl fields `language`, `text_normalization` and `fade_out_ms` are not sent.
  Sampling stays at the server defaults (temperature 0.75, top-k 80, top-p 0.69, repetition 2.4 over 96 frames and
  6 codebooks); `[cpp.params]` adds fields (`speaker_cfg_scale = 1.5` doubles the computing).
- No default voices: `VoiceSet(presets=False)`, so the narrator uses the Imperial male clip, and at least one clip
  is needed. Generic lines share a clip as on the cloud. The cache tag is
  `zonos3.cpp:<gguf name>:<clone>:<params>`.
- One request at a time (`[tts] workers = 1`, the book pool follows), with the growing split of the old local
  backend (`first_min=30, first_max=90, growth=1.6, single_max=100`).
- Speed on the laptop (Ryzen AI 7 350, CPU, q8_0, `--dac-threads 8`, warmed server): 2.9 s of audio in 6 s, 4.7 s
  in 6.3 s, 16.2 s (3 parts) in 11.6 s; the decoder runs at RTF 0.55-0.58, the rest is the backbone and about 2 s
  per request. Two requests at once are served one after the other. Memory: RSS about 6 GB (the laptop has 14 GB).
  Through `zonos3-cli` the same model took 20 s for 5.6 s including load (2026-10-27).

### Zyphra Cloud (`zyphra`)

Facts checked with real calls on 2026-11-10:

- Base URL `https://api.zyphracloud.com/api/v1`, header `Authorization: Bearer <key>`. Cloudflare rejects the
  default Python User-Agent, so the client sends `zonosvoice/<version>`. OpenAPI: `/api/openapi.json`.
- Cloning: `POST /audio/codes` (multipart `audio` + `model`) gives the clip's codec codes (`codes`: a base64 `.npy`,
  int16 `[10, frames]` at 15 frames/s; 4 KB for a 10 s clip; about 4 s per clip; the first 60 s of the clip). The
  request sends it as `speaker_codes_base64`. This is the faithful clone and the default (`[tts] clone = "codes"`).
  `POST /audio/embedding` with `model` gives a 192-value `.npy` embedding (`speaker_embedding_base64`): a looser
  clone that allows `bandwidth_bucket` and `loudness_bucket`. The result is kept in
  `<cache>/speakers/ZONOS3-Mini/<clip hash>.<codes|embedding>.json` (the ZONOS2 embeddings of 0.3.0 are in
  `<cache>/embeddings/`, now unused).
- `POST /audio/speech`: `model`, `input`, `response_format: "pcm"` (float32 LE mono 44.1 kHz), `seed`, and
  `voice` (a default voice name) or the codes/embedding. `text_norm` is refused by ZONOS3 Mini (HTTP 400
  `unsupported_parameter`: the model reads the text as written), so the request omits it. Whisper check: "500
  drakes", "the 3rd of Hearthfire", "9 pm" and "Dr." are read correctly without it. Unknown request fields are
  ignored. Sampling defaults of the API: temperature 1.15, top-k 106, min-p 0.18, `expressive: false`.
- A 786-byte input was accepted (55 s of audio in 4.4 s); the 600-byte limit of ZONOS2 does not apply. The parts
  stay at most 280 characters anyway, for a short wait.
- Wait: about 2.0 to 2.2 s per request for a 3 s line, also with four requests at once (the account allows parallel
  requests; `[tts] workers` stays 6). `samples` (21 lines, 6 workers, codes and speech requests together) took 20 s
  and hit one HTTP 429, which `http()` retried after 1 s.
- Default voices (`GET /audio/default-voices`, 27): the ZONOS2 names (`british-male`, ...) are gone. An unknown name is
  HTTP 400 `voice_not_found` (ZONOS2 fell back silently). English: savannah (en-US f), wyatt (en-US m), imogen
  (en-GB f), rupert (en-GB m), cillian (en-IE m), thaddeus, jett (m), sakura, morgana, willow (f). An unknown model
  or one the account cannot use is HTTP 404 `model_not_found`.
- The cache tag is `zyphra:zyphra/ZONOS3-Mini:<clone>:<params>`, so lines made by 0.3.0 (ZONOS2) are not reused.

## Engine facts (OpenMW 0.51, checked in the source and in game)

- The VFS indexes the data folders once, at start (`FileSystemArchive`). Each `open()` reads the file from disk.
  Therefore the 48 slot files must exist before the game starts, and the daemon replaces their contents (write to
  a temporary file, then rename).
- Lua `print` goes to `openmw.log` at once. It is the only channel from Lua to the outside. OpenMW truncates the
  log at each start, and the daemon notices a changed first line.
- The dialogue window pauses the game. Event handlers and the player script's `onFrame` still run.
  `core.sound.say` also plays while the game is paused.
- A local script can call `core.sound.say` only on itself, so the global script plays the parts.
- `DialogueResponse` (player event) gives `type`, `recordId`, `infoId` and `actor`. The Lua script finds the text
  in `core.dialogue[type].records[recordId].infos` and fills the `%` variables itself.
- `UiModeChanged` gives the book object as `arg` for the `Book` and `Scroll` modes.

## Voices: checks

- `extract-game-voices` uses only `Morrowind.esm` Hello lines. The records and the files differ in letter case, so
  the files are looked up in lower case.
- Speaker check (SpeechBrain ECAPA, 2026-10-15, VP backend): for all 20 race and sex samples, the clone is closest
  to its own game actor (cosine 0.61 to 0.82). Argonian male and Khajiit male tie at 0.74, and Breton female is
  near High Elf female (0.70); those pairs may share an actor.
- Temperature 0.65 on minisgl: pitch drift across seeds is rare for most actors. The Wood Elf male actor varies a
  lot, even in the reference itself.

## End-to-end test

    ./test_ingame.py [--plugin ../CaiusVoiced/CaiusVoiced.esp] [--npc "<cell>=<id>"] [--book <id>[:<s>]]

- The test starts the daemon (`run --trace`) and starts OpenMW in Xephyr (`:77`) with the Checks-Twice test save.
- OpenAL writes the game sound to `test_out/game_audio.wav` (its `wave` driver).
- A test script talks to 12 NPCs (all 10 races, Caius, and Jold from NestedInBalmora), then opens two books.
- Each part that the daemon made is found in the recorded sound by cross-correlation (played: correlation 0.9 or
  more).
- Needs the repository's `tools/` (harness helpers), Xephyr (unpacked in `~/.local/opt/xephyr`), and an empty
  `mwtest.omwscripts` for the test save (the test makes it).
- Do not run it while you play: the test's OpenMW writes the same `openmw.log`.

Results:

| Date | Bank / backend | Lines | Parts made | Parts found in the game sound |
|---|---|---|---|---|
| 2026-10-14 | stock clips / vp | 13 | 28 | 23 |
| 2026-10-15 | game voices / vp | 13 | 27 | 22 |
| 2026-10-15 | game voices (voices/) / zyphra | 13 | 23 | 21 |
| 2026-10-19 | game voices / local (parallel requests, cloud split) | 13 | 23 | 21 |
| 2026-10-19 | game voices / local (one request at a time, growing parts) | 13 | 38 | 36 |
| 2026-11-10 | game voices / Zyphra Cloud ZONOS3-Mini, codes clone (0.4.0) | 13 | 24 | 19 |
| 2026-11-10 | game voices / cpp (zonos3-q8_0, laptop CPU), prewarm in parallel | 12 | 34 | 29 |
| 2026-11-10 | game voices / cpp, prewarm yields to game lines (24 parts from the cache) | 12 | 17 | 36 of 41 |

Zyphra Cloud run: the first part of a new line was ready after 2.9 to 8.1 s (median about 4.4 s). Speaker check on
Zyphra Cloud samples: 20/20 closest to their own actor, median cosine 0.69.

ZONOS3-Mini run (2026-11-10): the first part of a new line was ready after 1.9 to 3.2 s (most about 2.2 s); all
19 parts that played had correlation 1.00; 24 API requests, 0 failed. The 5 parts not found are the Sithis cut.

cpp runs (2026-11-10, laptop CPU, the test's OpenMW on llvmpipe): in the first run the prewarm (68 clips, 267 s) shared
the serial server with the game, and first parts took 21-35 s until it was done, then 4.3-7.4 s. The prewarm now
waits while a game part is queued (`Daemon.inflight`); in the second run new first parts took 7.5-15.4 s with the
prewarm still running and the game rendering in software, and every part that played was found (corr 0.95-1.00;
the parts not found were cut by the test). 0 failed in both runs.

The parts that were not found are the "Sithis" parts 3 to 7: the test closes that book after 20 s on purpose.

Local backend runs: the test's OpenMW renders with llvmpipe (software OpenGL in Xephyr), which takes CPU from the
TTS server; generation then ran at RTF 0.7-1.8 instead of about 0.5, so these numbers are a worst case. With one
request at a time and growing parts, new first parts were ready after 1.8-3.1 s, except one 130-character line kept
as a single part (10.5 s; texts over 100 characters are now split), and every later part was ready before the
previous one ended. Without the game, a 57-character first part takes 2.7 s.

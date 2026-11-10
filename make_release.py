#!/usr/bin/env python3
"""Build the release zip: dist/ZonosVoice-<version>.zip, from an allowlist of source files only.

  python3 make_release.py

The build stops if a file in the list is missing, if any audio file other than the 48 silent slot files would go
into the zip, or if a file holds a local path, an internal host name or an API key. Voice clips are made by each
player from their own game (extract-game-voices); they must never be in a release.
"""
import io, os, re, sys, wave, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = [
    'README.md',
    'ZonosVoice.omwscripts',
    'scripts/zonosvoice/player.lua',
    'scripts/zonosvoice/global.lua',
    'l10n/ZonosVoice/en.yaml',
    'zonosvoice.py',
    'lexicon.tsv',
    'config.example.toml',
    'voices/README.md',
]
OPTIONAL = ['LICENSE', 'LICENSE.md', 'LICENSE.txt']
# The 48 slot files the daemon fills while the game runs. OpenMW indexes data files only at start, so they ship in
# the zip (0.1 s of silence each). They are made here, not copied: they are the only audio a release may hold.
SLOTS = [f'Sound/Vo/zonosvoice/s{i:02d}.wav' for i in range(48)]


def silent_wav():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b'\0\0' * 4410)
    return buf.getvalue()


def is_silent_slot(data):
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() <= 44100 and not any(w.readframes(w.getnframes()))


AUDIO = re.compile(r'\.(wav|mp3|ogg|flac|opus|m4a|aac|bsa)$', re.I)
FORBIDDEN = [
    (re.compile(r'/home/|/data/home/|C:\\\\Users\\\\[A-Za-z]'), 'a local path'),
    (re.compile(r'vp-dgx|dgxh100|sgl_rawclone|gradio\.live'), 'an internal host name'),
    (re.compile(r'\b(zk|sk)-[A-Za-z0-9]{20,}|Bearer [A-Za-z0-9._-]{20,}'), 'an API key'),
]


def main():
    sys.path.insert(0, HERE)
    version = re.search(r"^VERSION = '([^']+)'", open(os.path.join(HERE, 'zonosvoice.py')).read(), re.M).group(1)
    files = FILES + [f for f in OPTIONAL if os.path.exists(os.path.join(HERE, f))]
    problems = []
    for rel in files:
        path = os.path.join(HERE, rel)
        if not os.path.isfile(path):
            problems.append(f'{rel}: missing')
            continue
        if AUDIO.search(rel):
            problems.append(f'{rel}: audio files must not be in a release')
            continue
        text = open(path, encoding='utf-8', errors='replace').read()
        for pattern, what in FORBIDDEN:
            m = pattern.search(text)
            if m:
                line = text.count('\n', 0, m.start()) + 1
                problems.append(f'{rel}:{line}: holds {what} ({m.group(0)[:30]})')
    if not any(f in files for f in OPTIONAL):
        print('warning: no LICENSE file')
    if problems:
        print('release NOT built:\n  ' + '\n  '.join(problems))
        sys.exit(1)
    os.makedirs(os.path.join(HERE, 'dist'), exist_ok=True)
    out = os.path.join(HERE, 'dist', f'ZonosVoice-{version}.zip')
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            z.write(os.path.join(HERE, rel), f'ZonosVoice/{rel}')
        slot = silent_wav()
        for rel in SLOTS:
            z.writestr(f'ZonosVoice/{rel}', slot)
        z.writestr('ZonosVoice/zonosvoice/ready.txt', 'status idle\n')
    with zipfile.ZipFile(out) as z:   # check the result, not only the input list
        names = z.namelist()
        bad = [n for n in names if AUDIO.search(n) and not (n[len('ZonosVoice/'):] in SLOTS and is_silent_slot(z.read(n)))]
        if bad:
            os.remove(out)
            sys.exit(f'audio found in the zip: {bad}')
    print(f'{out}: {len(names)} files, {os.path.getsize(out) // 1024} KB')
    for n in names:
        if not n.startswith('ZonosVoice/Sound/Vo/zonosvoice/s') or n.endswith('s00.wav'):
            print('  ' + n + ('  (and s01 to s47: silent slot files)' if n.endswith('s00.wav') else ''))


if __name__ == '__main__':
    main()

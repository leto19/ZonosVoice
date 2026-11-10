#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = ["numpy", "scipy", "soundfile"]
# ///
"""Developer tool (not in the release): test the companion launcher with the real OpenMW launcher.

  ./test_launcher.py

1. Copies your OpenMW config (openmw.cfg, settings.cfg, launcher.cfg) into test_out_launcher/xdg, so the test does
   not change your own config (XDG_CONFIG_HOME points there). Removes the ZonosVoice lines from that openmw.cfg, so
   the companion must add them again (the first-run setup). Adds the test save, the test script and skip-menu.
2. Starts Xephyr (:77) and `zonosvoice.py launcher --trace`, which opens the real openmw-launcher.
3. Clicks the launcher's "Launch OpenMW" button (XTEST). The launcher starts the game and closes, as it always does.
4. The test script in the game talks to some NPCs and opens a book, then quits the game.
5. Checks: the companion exits by itself after the game exits; each part was played (found in the recorded game
   sound, as in test_ingame.py).
"""
import json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_ingame as ti   # noqa: E402  (Lua test scripts, report, harness helpers)

OUT = os.path.join(HERE, 'test_out_launcher')
NPCS = ['Balmora, Eight Plates=dulnea ralaal', 'Balmora, Lucky Lockup=hickim']
BOOKS = ['bk_clientlist']
XINPUT = os.path.join(ti.REPO, 'tools', 'xinput.py')
CACHE_LOG = None


def window_geometry(name):
    out = subprocess.run(['xwininfo', '-root', '-tree'], capture_output=True, text=True,
                         env=dict(os.environ, DISPLAY=':77')).stdout
    m = re.search(rf'"{re.escape(name)}": \(.*?\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', out)
    return tuple(int(v) for v in m.groups()) if m else None


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    xdg = os.path.join(OUT, 'xdg', 'openmw')
    data = os.path.join(OUT, 'data')
    os.makedirs(xdg)
    os.makedirs(os.path.join(data, 'scripts', 'zve2e'))
    src = os.path.expanduser('~/.config/openmw')
    for f in ('openmw.cfg', 'settings.cfg', 'launcher.cfg'):
        shutil.copyfile(os.path.join(src, f), os.path.join(xdg, f))
    # The launcher window must be inside the 1280x720 Xephyr screen.
    lcfg = open(os.path.join(xdg, 'launcher.cfg')).read()
    lcfg = re.sub(r'MainWindow/posx=.*', 'MainWindow/posx=100', re.sub(r'MainWindow/posy=.*', 'MainWindow/posy=40', lcfg))
    open(os.path.join(xdg, 'launcher.cfg'), 'w').write(lcfg)
    lines = [l for l in open(os.path.join(xdg, 'openmw.cfg')).read().splitlines() if 'zonosvoice' not in l.lower()]
    plan = ', '.join(['{ cell = %s, id = %s }' % (json.dumps(c), json.dumps(i)) for c, i in (n.split('=', 1) for n in NPCS)] +
                     ['{ book = %s, limit = nil }' % json.dumps(b) for b in BOOKS])
    open(os.path.join(data, 'scripts/zve2e/player.lua'), 'w').write(ti.PLAYER % dict(plan=plan, talk_timeout=45.0))
    open(os.path.join(data, 'scripts/zve2e/global.lua'), 'w').write(ti.GLOBAL)
    open(os.path.join(data, 'ZVE2E.omwscripts'), 'w').write('PLAYER: scripts/zve2e/player.lua\nGLOBAL: scripts/zve2e/global.lua\n')
    open(os.path.join(data, 'mwtest.omwscripts'), 'w').write('')
    lines += [f'data="{data}"', 'content=TestCharacter.esp', 'content=mwtest.omwscripts', 'content=ZVE2E.omwscripts',
              'skip-menu=1', f'load-savegame="{ti.SAVE}"']
    open(os.path.join(xdg, 'openmw.cfg'), 'w').write('\n'.join(lines) + '\n')
    audio = os.path.join(OUT, 'game_audio.wav')
    conf = os.path.join(OUT, 'alsoft.conf')
    open(conf, 'w').write(f'[general]\ndrivers = wave\nchannels = stereo\nfrequency = 44100\n[wave]\nfile = {audio}\n')

    if ti_game_running():
        sys.exit('an OpenMW game is running: close it first (the companion would wait for it)')
    xephyr = subprocess.Popen(['Xephyr', ':77', '-screen', '1280x720', '-ac', '-br', '-noreset'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    env = dict(os.environ, DISPLAY=':77', QT_QPA_PLATFORM='xcb', SDL_VIDEODRIVER='x11', XDG_CONFIG_HOME=os.path.dirname(xdg),
               ALSOFT_CONF=conf, ZONOSVOICE_STATS=os.path.join(OUT, 'stats.json'))
    env.pop('WAYLAND_DISPLAY', None)
    t0 = time.time()
    companion = subprocess.Popen([sys.executable, os.path.join(HERE, 'zonosvoice.py'), 'launcher', '--trace'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    geom = None
    while time.time() - t0 < 60 and not geom:
        time.sleep(1)
        geom = window_geometry('OpenMW Launcher')
    if not geom:
        companion.kill(); xephyr.terminate()
        sys.exit('the launcher window did not appear')
    time.sleep(3)
    subprocess.run(['import', '-display', ':77', '-window', 'root', os.path.join(OUT, 'launcher.png')])
    w, h, x, y = geom
    bx, by = x + w - 89, y + h - 22   # the "Launch OpenMW" button, bottom right of the window
    print(f'launcher window {w}x{h}+{x}+{y}; clicking Launch OpenMW at {bx},{by}', flush=True)
    subprocess.run([sys.executable, XINPUT, 'click', str(bx), str(by)], env=dict(os.environ, XINPUT_DISPLAY=':77'))
    t_click = time.time()
    game_seen = game_gone = None
    while companion.poll() is None and time.time() - t_click < 600:
        running = ti_game_running()
        if running and not game_seen:
            game_seen = time.time()
        if game_seen and not running and not game_gone:
            game_gone = time.time()
        time.sleep(0.5)
    t_exit = time.time()
    if companion.poll() is None:
        companion.kill()
        print('the companion did not exit in time; killed')
    xephyr.terminate()
    print(f'game started {game_seen - t_click:.1f} s after the click; ran {game_gone - game_seen:.1f} s; '
          f'companion exited {t_exit - game_gone:.1f} s after the game' if game_seen and game_gone else
          f'game seen: {bool(game_seen)}, game gone: {bool(game_gone)}')
    cfg_now = open(os.path.join(xdg, 'openmw.cfg')).read()
    print('openmw.cfg after the run: ZonosVoice data line', 'present' if 'ZonosVoice"' in cfg_now else 'MISSING',
          '| content line', 'present' if 'content=ZonosVoice.omwscripts' in cfg_now else 'MISSING',
          '| skip-menu kept' if 'skip-menu' in cfg_now else '| skip-menu dropped by the launcher')
    cache_log = os.path.join(os.environ.get('XDG_CACHE_HOME', os.path.expanduser('~/.cache')), 'zonosvoice', 'launcher.log')
    shutil.copyfile(cache_log, os.path.join(OUT, 'daemon.log'))
    shutil.copyfile(os.path.join(xdg, 'openmw.log'), os.path.join(OUT, 'openmw.log'))
    print('--- companion log (first lines) ---')
    for line in open(os.path.join(OUT, 'daemon.log')).read().splitlines()[:12]:
        if not line.startswith('TRACE'):
            print('  ' + line[:160])
    ti.report(OUT, audio)


def ti_game_running():
    for pid in os.listdir('/proc'):
        if pid.isdigit():
            try:
                if open(f'/proc/{pid}/comm').read().strip() in ('openmw', 'openmw.x86_64'):
                    return True
            except OSError:
                pass
    return False


if __name__ == '__main__':
    main()

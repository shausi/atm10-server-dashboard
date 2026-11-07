"""ATM10 Dashboard: local web panel for the All the Mods 10 server on this PC.
Runs on http://localhost:7870 only. Python standard library, no installs.
"""
import os, sys, json, re, time, threading, subprocess, shutil, zipfile, hashlib, secrets, ctypes
import datetime, urllib.request, urllib.parse, urllib.error, collections, tomllib, traceback, socket
from ctypes import wintypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE = os.environ.get('ATM10_BASE', r'D:\MinecraftServers')
ROOT = os.environ.get('ATM10_ROOT', os.path.join(BASE, 'ATM10'))
BACKUPS = os.path.join(BASE, 'Backups')
SHARE = os.path.join(BASE, 'Share')
TRASH = os.path.join(BASE, '_trash')
APPDIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(APPDIR, 'data')
EXTRAS_FILE = os.path.join(BASE, 'ATM10_extra_mods.json')
VERSION_FILE = os.path.join(BASE, 'ATM10_version.txt')
KEY_FILE = os.path.join(BASE, 'curseforge_key.txt')
SCHED_FILE = os.path.join(DATA, 'schedules.json')
SETTINGS_FILE = os.path.join(DATA, 'settings.json')
CLIENT_MODS = os.path.join(os.environ.get('USERPROFILE', ''), r'curseforge\minecraft\Instances\All the Mods 10 - ATM10\mods')
PORT = 7870
GAME_PORT = 25565
TOKEN = secrets.token_urlsafe(24)
NOWIN = 0x08000000
UA = {'User-Agent': 'ATM10-Dashboard/1.0 (local server tool)'}
os.makedirs(DATA, exist_ok=True)


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def now_stamp():
    return datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')


# ---------------------------------------------------------------- Windows process stats
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class PMC(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]


psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4


class MEMSTAT(ctypes.Structure):
    _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('sullAvailExtendedVirtual', ctypes.c_ulonglong)]


def proc_stats(pid):
    h = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not h:
        return None
    try:
        c = PMC()
        c.cb = ctypes.sizeof(c)
        ws = 0
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), ctypes.sizeof(c)):
            ws = c.WorkingSetSize
        cr, ex, kr, us = (wintypes.FILETIME() for _ in range(4))
        cpu100 = 0
        if kernel32.GetProcessTimes(h, ctypes.byref(cr), ctypes.byref(ex), ctypes.byref(kr), ctypes.byref(us)):
            cpu100 = ((kr.dwHighDateTime << 32) | kr.dwLowDateTime) + ((us.dwHighDateTime << 32) | us.dwLowDateTime)
        return ws, cpu100
    finally:
        kernel32.CloseHandle(h)


def system_memory():
    m = MEMSTAT()
    m.dwLength = ctypes.sizeof(m)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullTotalPhys, m.ullTotalPhys - m.ullAvailPhys


_port_cache = {'t': 0, 'pid': None}


def listening_pid():
    if time.time() - _port_cache['t'] < 2:
        return _port_cache['pid']
    pid = None
    try:
        out = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True, text=True, creationflags=NOWIN, timeout=10).stdout
        for line in out.splitlines():
            m = re.match(r'^\s*TCP\s+\S*:%d\s+\S+\s+LISTENING\s+(\d+)' % GAME_PORT, line)
            if m:
                pid = int(m.group(1))
                break
    except Exception:
        pid = None
    _port_cache.update(t=time.time(), pid=pid)
    return pid


# ---------------------------------------------------------------- server process
class Server:
    def __init__(self):
        self.p = None
        self.lines = collections.deque(maxlen=8000)
        self.seq = 0
        self.lock = threading.Lock()
        self.ready = False
        self.started = None
        self.players = set()
        self.tps = None
        self.tps_ok = True
        self.hide_until = 0.0
        self.last_saved_seq = 0

    def log(self, text):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, text))

    def running(self):
        return self.p is not None and self.p.poll() is None

    def start(self):
        if self.running():
            raise ApiError('The server is already running.')
        if listening_pid():
            raise ApiError('A server is already running outside the dashboard. Stop it first.')
        env = dict(os.environ, ATM10_RESTART='false')
        self.ready = False
        self.players = set()
        self.tps = None
        self.tps_ok = True
        self.p = subprocess.Popen(['cmd.exe', '/c', os.path.join(ROOT, 'startserver.bat')], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NOWIN)
        self.started = time.time()
        self.log('--- starting server ---')
        threading.Thread(target=self._read, args=(self.p,), daemon=True).start()

    def _read(self, p):
        for raw in iter(p.stdout.readline, b''):
            self._parse(raw.decode('utf-8', 'replace').rstrip('\r\n'))
        code = p.wait()
        self.ready = False
        self.players = set()
        self.log('--- server stopped (exit code %s) ---' % code)

    def _parse(self, line):
        if time.time() < self.hide_until:
            m = re.search(r'Overall:\s*([\d.]+) TPS', line) or re.search(r'Overall:.*?Mean TPS:\s*([\d.]+)', line)
            if m:
                self.tps = float(m.group(1))
                return
            if re.search(r'[\d.]+ TPS \([\d.]+ ms/tick\)|Mean tick time|Mean TPS', line):
                return
            if 'Unknown or incomplete command' in line or 'Unknown command' in line:
                self.tps_ok = False
                return
        if re.search(r'Done \([\d.]+s\)! For help', line):
            self.ready = True
        m = re.search(r': (\w{3,16}) joined the game$', line)
        if m:
            self.players.add(m.group(1))
        m = re.search(r': (\w{3,16}) left the game$', line)
        if m:
            self.players.discard(m.group(1))
        self.log(line)
        if 'Saved the game' in line:
            self.last_saved_seq = self.seq

    def send(self, cmd):
        if not self.running():
            raise ApiError('The server is not running from the dashboard.')
        self.p.stdin.write((cmd + '\n').encode('utf-8'))
        self.p.stdin.flush()

    def stop(self):
        self.send('stop')

    def kill(self):
        if self.running():
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(self.p.pid)], capture_output=True, creationflags=NOWIN)

    def wait_exit(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if not self.running():
                return True
            time.sleep(0.5)
        return not self.running()

    def since(self, n):
        with self.lock:
            return [(s, t) for s, t in self.lines if s > n], self.seq


SRV = Server()


def server_state():
    if SRV.running():
        return 'running' if SRV.ready else 'starting'
    if listening_pid():
        return 'external'
    return 'stopped'


_cpu_prev = {}


def read_props():
    props = []
    try:
        with open(os.path.join(ROOT, 'server.properties'), 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\r\n')
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                props.append((k, v))
    except FileNotFoundError:
        pass
    return props


def get_prop(key, default=None):
    for k, v in read_props():
        if k == key:
            return v
    return default


def read_xmx():
    try:
        with open(os.path.join(ROOT, 'user_jvm_args.txt'), 'r', encoding='utf-8') as f:
            m = re.search(r'^\s*-Xmx(\d+)([GMgm])', f.read(), re.M)
        if m:
            n = int(m.group(1))
            return n * (1024 ** 3 if m.group(2).upper() == 'G' else 1024 ** 2), '%d%s' % (n, m.group(2).upper())
    except Exception:
        pass
    return 0, ''


def installed_version():
    try:
        v = open(VERSION_FILE, 'r').read().strip()
        if v:
            return v
    except Exception:
        pass
    return None


def status():
    st = server_state()
    pid = listening_pid()
    cpu = ram = None
    if pid and st in ('running', 'starting', 'external'):
        s = proc_stats(pid)
        if s:
            ram = s[0]
            prev = _cpu_prev.get(pid)
            tnow = time.time()
            if prev and tnow > prev[0]:
                cpu = max(0.0, (s[1] - prev[1]) / 1e7 / (tnow - prev[0]) / (os.cpu_count() or 1) * 100)
            _cpu_prev.clear()
            _cpu_prev[pid] = (tnow, s[1])
    tot, used = system_memory()
    du = shutil.disk_usage(BASE)
    xmx_b, xmx_s = read_xmx()
    mods_dir = os.path.join(ROOT, 'mods')
    mods = len([f for f in os.listdir(mods_dir) if f.lower().endswith('.jar')]) if os.path.isdir(mods_dir) else 0
    return {
        'state': st, 'uptime': int(time.time() - SRV.started) if st in ('running', 'starting') and SRV.started else 0,
        'players': sorted(SRV.players), 'max_players': get_prop('max-players', '20'),
        'tps': SRV.tps, 'cpu': cpu, 'ram_used': ram, 'ram_max': xmx_b, 'xmx': xmx_s,
        'sys_ram_total': tot, 'sys_ram_used': used, 'disk_total': du.total, 'disk_used': du.used,
        'version': installed_version(), 'cores': os.cpu_count(), 'mods': mods,
        'motd': get_prop('motd', ''), 'whitelist': get_prop('white-list', 'false'),
    }


def tps_poller():
    while True:
        time.sleep(15)
        try:
            if SRV.running() and SRV.ready and SRV.tps_ok:
                SRV.hide_until = time.time() + 4
                SRV.send('neoforge tps')
        except Exception:
            pass


# ---------------------------------------------------------------- jobs
JOBS = {}
OP_LOCK = threading.Lock()


def run_job(title, fn, needs_lock=True):
    jid = secrets.token_hex(4)
    job = {'id': jid, 'title': title, 'log': [], 'done': False, 'ok': None, 'result': None, 't': time.time()}
    JOBS[jid] = job
    for old in [k for k, v in JOBS.items() if v['done'] and time.time() - v['t'] > 3600]:
        JOBS.pop(old, None)

    def log(m):
        job['log'].append(m)

    def runner():
        got = OP_LOCK.acquire(blocking=False) if needs_lock else True
        if not got:
            log('Another task is still running. Try again when it finishes.')
            job.update(done=True, ok=False)
            return
        try:
            job['result'] = fn(log)
            job['ok'] = True
        except Exception as e:
            log('FAILED: %s' % e)
            job['ok'] = False
            if not isinstance(e, ApiError):
                log(traceback.format_exc(limit=3))
        finally:
            job['done'] = True
            if needs_lock:
                OP_LOCK.release()

    threading.Thread(target=runner, daemon=True).start()
    return jid


# ---------------------------------------------------------------- http helpers
def http(url, method='GET', body=None, headers=None, timeout=30, raw=False):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data)


def download(url, dest, sha1=None, log=None):
    req = urllib.request.Request(url, headers=UA)
    h = hashlib.sha1()
    tmp = dest + '.part'
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        last = time.time()
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if log and total and time.time() - last > 3:
                log('downloaded %d%%' % (got * 100 // total))
                last = time.time()
    if sha1 and h.hexdigest().lower() != sha1.lower():
        os.remove(tmp)
        raise ApiError('Download failed its hash check: %s' % os.path.basename(dest))
    os.replace(tmp, dest)


# ---------------------------------------------------------------- mods: scan + names
JAR_CACHE = {}
HASH_PROJ = {}


def ckey(s):
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


def name_keys(title, slug):
    t = (title or '').lower()
    s = (slug or '').lower()
    variants = [t, s, re.sub(r"['\u2019]s", '', t), re.sub(r'\s*\([^)]*\)', '', t).strip()]
    variants += re.findall(r'\(([^)]+)\)', t)
    for v in list(variants):
        v2 = re.sub(r'[\s\-_]*(api|lib|library|core lib|neoforge/forge|neoforge|forge|fabric)$', '', v)
        if v2 != v:
            variants.append(v2)
    return {ckey(v) for v in variants if len(ckey(v)) >= 3}


def scan_jar(path):
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    c = JAR_CACHE.get(path)
    if c and c['key'] == key:
        return c
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    mods = []
    try:
        with zipfile.ZipFile(path) as z:
            name = 'META-INF/neoforge.mods.toml' if 'META-INF/neoforge.mods.toml' in z.namelist() else 'META-INF/mods.toml'
            if name in z.namelist():
                txt = z.read(name).decode('utf-8', 'replace')
                try:
                    data = tomllib.loads(txt)
                    for m in data.get('mods', []):
                        mods.append({'modId': m.get('modId', ''), 'displayName': m.get('displayName', ''), 'version': str(m.get('version', ''))})
                except Exception:
                    for blk in re.split(r'(?m)^\s*\[\[', txt):
                        if blk.startswith('mods]]'):
                            mid = re.search(r'(?m)^\s*modId\s*=\s*"([^"]+)"', blk)
                            dn = re.search(r'(?m)^\s*displayName\s*=\s*"([^"]+)"', blk)
                            mods.append({'modId': mid.group(1) if mid else '', 'displayName': dn.group(1) if dn else '', 'version': ''})
    except Exception:
        pass
    c = {'key': key, 'sha1': h.hexdigest(), 'mods': mods}
    JAR_CACHE[path] = c
    return c


def mods_dir():
    return os.path.join(ROOT, 'mods')


def installed_state():
    ids, names = set(), set()
    d = mods_dir()
    jars = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith('.jar')] if os.path.isdir(d) else []
    unknown = []
    for p in jars:
        try:
            c = scan_jar(p)
        except Exception:
            continue
        if c['sha1'] not in HASH_PROJ:
            unknown.append(c['sha1'])
        for m in c['mods']:
            for v in (m['modId'], m['displayName'], re.sub(r"['\u2019]s", '', m['displayName'])):
                k = ckey(v)
                if k:
                    names.add(k)
                    names.add('d:' + re.sub(r'\d', '', k))
    for i in range(0, len(unknown), 500):
        chunk = unknown[i:i + 500]
        try:
            res = http('https://api.modrinth.com/v2/version_files', 'POST',
                       json.dumps({'hashes': chunk, 'algorithm': 'sha1'}).encode(), {'Content-Type': 'application/json'}, 60)
        except Exception:
            res = {}
        for hsh in chunk:
            HASH_PROJ[hsh] = res.get(hsh, {}).get('project_id') if isinstance(res.get(hsh), dict) else None
    for p in jars:
        c = JAR_CACHE.get(p)
        if c and HASH_PROJ.get(c['sha1']):
            ids.add(HASH_PROJ[c['sha1']])
    return ids, names


def is_installed(pid, title, slug, state):
    ids, names = state
    if pid in ids:
        return True
    for k in name_keys(title, slug):
        if len(k) >= 4 and k in names:
            return True
        d = re.sub(r'\d', '', k)
        if len(d) >= 8 and ('d:' + d) in names:
            return True
    return False


def get_extras():
    data = read_json(EXTRAS_FILE, [])
    if isinstance(data, dict):
        data = [data]
    flat = []
    for x in data:
        if isinstance(x, list):
            flat.extend(x)
        else:
            flat.append(x)
    return [x for x in flat if isinstance(x, dict) and x.get('filename')]


def add_extra(filename, project_id):
    items = [x for x in get_extras() if x['filename'] != filename]
    items.append({'filename': filename, 'projectId': project_id, 'addedAt': datetime.date.today().isoformat()})
    write_json(EXTRAS_FILE, items)


RECOMMENDED = [
    ('chunky', 'Pre-generates the map so exploring does not lag. Server only.'),
    ('luckperms', 'Permissions and ranks, so friends cannot use admin commands. Server only.'),
    ('simple-voice-chat', 'Proximity voice chat. Everyone needs it, and UDP port 24454 must be forwarded.'),
    ('neruina', 'Stops one broken entity or block from crashing the whole server.'),
    ('worldedit', 'Admin building tool for big edits and repairs. Server only.'),
    ('clumps', 'Merges XP orbs, which cuts lag around big farms.'),
    ('no-chat-reports', 'Keeps chat private from Mojang chat reporting.'),
    ('alternate-current', 'Faster redstone. Test it, it may overlap with the pack.'),
    ('servercore', 'Server performance tuning. Test it, it may overlap with the pack.'),
]
FACETS = '[["project_type:mod"],["categories:neoforge"],["versions:1.21.1"],["server_side:required","server_side:optional"]]'


def hit_dict(h, state, why=None):
    pid = h.get('project_id') or h.get('id')
    return {'project_id': pid, 'title': h.get('title'), 'slug': h.get('slug'), 'downloads': h.get('downloads', 0),
            'server_side': h.get('server_side'), 'client_side': h.get('client_side'),
            'description': why or h.get('description', ''), 'icon': h.get('icon_url'),
            'installed': is_installed(pid, h.get('title'), h.get('slug'), state)}


def mods_search(q, offset):
    state = installed_state()
    index = 'relevance' if q.strip() else 'downloads'
    url = 'https://api.modrinth.com/v2/search?query=%s&facets=%s&limit=50&offset=%d&index=%s' % (
        urllib.parse.quote(q.strip()), urllib.parse.quote(FACETS), offset, index)
    r = http(url)
    return {'total': r.get('total_hits', 0), 'offset': offset + len(r.get('hits', [])),
            'hits': [hit_dict(h, state) for h in r.get('hits', [])]}


def mods_recommended():
    state = installed_state()
    ids = json.dumps([s for s, _ in RECOMMENDED])
    projs = http('https://api.modrinth.com/v2/projects?ids=' + urllib.parse.quote(ids))
    by_slug = {p['slug']: p for p in projs}
    out = []
    for slug, why in RECOMMENDED:
        p = by_slug.get(slug)
        if p:
            out.append(hit_dict(p, state, why))
    return {'hits': out}


def install_project(pid, have, log, to_client, depth=0):
    if pid in have:
        log('already installed (project %s), skipped' % pid)
        return
    proj = http('https://api.modrinth.com/v2/project/' + pid)
    state = (set(have), installed_state()[1])
    if is_installed(pid, proj.get('title'), proj.get('slug'), state):
        have.add(pid)
        log('%s is already installed, skipped' % proj.get('title'))
        return
    vers = http('https://api.modrinth.com/v2/project/%s/version?loaders=%s&game_versions=%s' % (
        pid, urllib.parse.quote('["neoforge"]'), urllib.parse.quote('["1.21.1"]')))
    if not vers:
        raise ApiError('no NeoForge 1.21.1 version for %s' % proj.get('title'))
    v = vers[0]
    f = next((x for x in v['files'] if x.get('primary')), v['files'][0])
    fname = os.path.basename(f['filename'])
    dest = os.path.join(mods_dir(), fname)
    log('downloading %s' % fname)
    download(f['url'], dest, f['hashes'].get('sha1'))
    if to_client and os.path.isdir(CLIENT_MODS):
        shutil.copy2(dest, os.path.join(CLIENT_MODS, fname))
    have.add(pid)
    add_extra(fname, pid)
    log('installed %s' % fname)
    for d in v.get('dependencies', []):
        if d.get('dependency_type') == 'required' and d.get('project_id'):
            try:
                install_project(d['project_id'], have, log, to_client, depth + 1)
            except Exception as e:
                log('dependency problem: %s' % e)


def mods_install_job(pids, to_client):
    def fn(log):
        if server_state() != 'stopped':
            raise ApiError('Stop the server first.')
        ids, _ = installed_state()
        have = set(ids)
        for pid in pids:
            try:
                install_project(pid, have, log, to_client)
            except Exception as e:
                log('%s failed: %s' % (pid, e))
        log('Done. Start the server to load the new mods. Friends need the same mods: use Share.')
    return run_job('Install mods', fn)


def mods_list():
    extras = {x['filename']: x for x in get_extras()}
    out = []
    d = mods_dir()
    for f in sorted(os.listdir(d), key=str.lower):
        if not f.lower().endswith('.jar'):
            continue
        p = os.path.join(d, f)
        try:
            c = scan_jar(p)
        except Exception:
            c = {'mods': []}
        m = c['mods'][0] if c['mods'] else {}
        out.append({'file': f, 'size': os.path.getsize(p), 'modId': m.get('modId', ''), 'name': m.get('displayName', '') or f,
                    'version': '' if '${' in str(m.get('version', '')) else m.get('version', ''), 'added': f in extras})
    return out


def share_job():
    def fn(log):
        extras = [x for x in get_extras() if os.path.isfile(os.path.join(mods_dir(), x['filename']))]
        if not extras:
            raise ApiError('No added mods yet. Install a mod first.')
        os.makedirs(SHARE, exist_ok=True)
        work = os.path.join(SHARE, '_build')
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(os.path.join(work, 'mods'))
        for x in extras:
            shutil.copy2(os.path.join(mods_dir(), x['filename']), os.path.join(work, 'mods', x['filename']))
        lines = [('%s   https://modrinth.com/project/%s' % (x['filename'], x['projectId'])) if x.get('projectId') else x['filename'] for x in extras]
        open(os.path.join(work, 'mods_list.txt'), 'w', encoding='utf-8').write('\r\n'.join(lines))
        readme = ("EXTRA MODS FOR THE ATM10 SERVER\r\n===============================\r\n"
                  "You need the All the Mods 10 modpack in the CurseForge app, at the SAME version as the server.\r\n"
                  "These are the extra mods the server has on top of the pack.\r\n\r\n"
                  "EASY WAY\r\n1. Close Minecraft.\r\n2. Double-click Install.bat in this folder.\r\n"
                  "3. Start All the Mods 10 from the CurseForge app and join the server.\r\n\r\n"
                  "BY HAND (if Install.bat cannot find your profile)\r\n"
                  "1. In the CurseForge app, click the three dots on All the Mods 10 - ATM10.\r\n"
                  "2. Click Open Folder, then open the mods folder.\r\n"
                  "3. Copy every .jar from the mods folder next to this file into it.\r\n\r\n"
                  "If you ever use Repair Profile, CurseForge may delete these. Run Install.bat again.\r\n"
                  "mods_list.txt names every mod.\r\n")
        open(os.path.join(work, 'README.txt'), 'w', encoding='ascii', errors='replace').write(readme)
        bat = ('@echo off\r\nset "DEST=%USERPROFILE%\\curseforge\\minecraft\\Instances\\All the Mods 10 - ATM10\\mods"\r\n'
               'if not exist "%DEST%" (\r\n    echo Could not find your All the Mods 10 profile mods folder.\r\n'
               '    echo Copy the .jar files from the mods folder next to this file into your ATM10 mods folder by hand. See README.txt.\r\n'
               '    pause\r\n    exit /b 1\r\n)\r\ncopy /Y "%~dp0mods\\*.jar" "%DEST%\\"\r\necho.\r\n'
               'echo Done. Start All the Mods 10 from the CurseForge app.\r\npause\r\n')
        open(os.path.join(work, 'Install.bat'), 'w', encoding='ascii').write(bat)
        out = os.path.join(SHARE, 'ATM10_Extra_Mods.zip')
        if os.path.exists(out):
            os.remove(out)
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for dp, _, fs in os.walk(work):
                for f in fs:
                    full = os.path.join(dp, f)
                    z.write(full, os.path.relpath(full, work))
        shutil.rmtree(work, ignore_errors=True)
        log('Built %s (%d mods, %.1f MB)' % (out, len(extras), os.path.getsize(out) / 1048576))
        return out
    return run_job('Build friends pack', fn)


# ---------------------------------------------------------------- backups
def wait_saved(timeout=120):
    before = SRV.seq
    SRV.send('save-all flush')
    end = time.time() + timeout
    while time.time() < end:
        if SRV.last_saved_seq > before:
            return True
        time.sleep(0.5)
    return False


def make_backup(log, label='world'):
    st = server_state()
    if st == 'external':
        raise ApiError('The server runs outside the dashboard. Stop it first.')
    if st == 'starting':
        raise ApiError('Wait until the server has finished starting.')
    world = os.path.join(ROOT, 'world')
    if not os.path.isdir(world):
        raise ApiError('No world folder found.')
    os.makedirs(BACKUPS, exist_ok=True)
    out = os.path.join(BACKUPS, '%s_%s.zip' % (label, now_stamp()))
    live = st == 'running'
    if live:
        log('saving the world')
        SRV.send('save-off')
        if not wait_saved():
            log('the save did not confirm in time, continuing')
    skipped = 0
    try:
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for dp, _, fs in os.walk(world):
                for f in fs:
                    if f == 'session.lock':
                        continue
                    full = os.path.join(dp, f)
                    try:
                        z.write(full, os.path.join('world', os.path.relpath(full, world)))
                    except (PermissionError, OSError):
                        skipped += 1
    finally:
        if live:
            try:
                SRV.send('save-on')
            except Exception:
                pass
    log('backup saved: %s (%.1f MB%s)' % (os.path.basename(out), os.path.getsize(out) / 1048576, ', %d files skipped' % skipped if skipped else ''))
    return out


def prune_auto(keep):
    items = sorted([f for f in os.listdir(BACKUPS) if f.startswith('auto_') and f.endswith('.zip')])
    for f in items[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(BACKUPS, f))
        except OSError:
            pass


def backups_list():
    out = []
    if not os.path.isdir(BACKUPS):
        return out
    for f in os.listdir(BACKUPS):
        p = os.path.join(BACKUPS, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        isdir = os.path.isdir(p)
        size = st.st_size if not isdir else None
        out.append({'name': f, 'dir': isdir, 'size': size, 'time': st.st_mtime})
    out.sort(key=lambda x: x['time'], reverse=True)
    return out


def safe_backup(name):
    p = os.path.normpath(os.path.join(BACKUPS, name))
    if os.path.commonpath([p, BACKUPS]) != os.path.normpath(BACKUPS) or p == os.path.normpath(BACKUPS) or not os.path.exists(p):
        raise ApiError('Backup not found.', 404)
    return p


def restore_job(name):
    def fn(log):
        if server_state() != 'stopped':
            raise ApiError('Stop the server first.')
        src = safe_backup(name)
        world = os.path.join(ROOT, 'world')
        hold = os.path.join(BACKUPS, '_before_restore_' + now_stamp())
        os.makedirs(hold, exist_ok=True)
        if os.path.isdir(world):
            shutil.move(world, os.path.join(hold, 'world'))
            log('current world moved to %s' % hold)
        if os.path.isfile(src) and src.lower().endswith('.zip'):
            with zipfile.ZipFile(src) as z:
                names = [n for n in z.namelist() if n.startswith('world/')]
                if not names:
                    shutil.move(os.path.join(hold, 'world'), world)
                    raise ApiError('That zip has no world folder. Nothing changed.')
                for n in names:
                    z.extract(n, ROOT)
        elif os.path.isdir(src):
            if os.path.isfile(os.path.join(src, 'level.dat')):
                shutil.copytree(src, world)
            elif os.path.isfile(os.path.join(src, 'world', 'level.dat')):
                shutil.copytree(os.path.join(src, 'world'), world)
            else:
                shutil.move(os.path.join(hold, 'world'), world)
                raise ApiError('No world found in that backup. Nothing changed.')
        else:
            raise ApiError('Unsupported backup type.')
        log('restored %s' % name)
    return run_job('Restore backup', fn)


# ---------------------------------------------------------------- updates (CurseForge)
def cf_key():
    try:
        return open(KEY_FILE, 'r').read().strip() or None
    except Exception:
        return None


def cf_api(path):
    return http('https://api.curseforge.com' + path, headers={'x-api-key': cf_key(), 'Accept': 'application/json'})


def latest_pack():
    s = cf_api('/v1/mods/search?gameId=432&classId=4471&slug=all-the-mods-10')
    if not s.get('data'):
        raise ApiError('ATM10 was not found on CurseForge.')
    mod = s['data'][0]
    f = cf_api('/v1/mods/%s/files?pageSize=50' % mod['id'])
    files = [x for x in f.get('data', []) if x.get('releaseType') == 1 and not x.get('isServerPack')]
    files.sort(key=lambda x: x.get('fileDate', ''), reverse=True)
    if not files:
        raise ApiError('No release files were found for ATM10.')
    latest = files[0]
    m = re.search(r'\d+(?:\.\d+)+', latest.get('fileName', '')) or re.search(r'\d+(?:\.\d+)+', latest.get('displayName', ''))
    return {'modId': mod['id'], 'fileId': latest['id'], 'version': m.group(0) if m else None,
            'serverPackFileId': latest.get('serverPackFileId'), 'name': latest.get('displayName')}


def vkey(v):
    return tuple(int(x) for x in re.findall(r'\d+', v or ''))


def update_check():
    if not cf_key():
        return {'ok': False, 'reason': 'nokey'}
    lp = latest_pack()
    cur = installed_version()
    newer = bool(cur and lp['version'] and vkey(lp['version']) > vkey(cur))
    return {'ok': True, 'current': cur, 'latest': lp['version'], 'newer': newer, 'serverPack': bool(lp['serverPackFileId']),
            'modId': lp['modId'], 'serverPackFileId': lp['serverPackFileId']}


KEEP = {'world', 'server.properties', 'whitelist.json', 'ops.json', 'banned-ips.json', 'banned-players.json', 'eula.txt',
        'usercache.json', 'startserver.bat', 'user_jvm_args.txt', 'logs', 'crash-reports', 'backups'}


def apply_pack(zip_path, version, log):
    if server_state() != 'stopped':
        raise ApiError('Stop the server first.')
    tmp = os.path.join(BASE, '_pack_tmp')
    shutil.rmtree(tmp, ignore_errors=True)
    log('extracting the pack')
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    src = tmp
    if not os.path.isdir(os.path.join(src, 'mods')):
        for d in os.listdir(tmp):
            if os.path.isdir(os.path.join(tmp, d, 'mods')):
                src = os.path.join(tmp, d)
                break
    if not os.path.isdir(os.path.join(src, 'mods')):
        raise ApiError('That zip has no mods folder. It does not look like a server pack.')
    stamp = now_stamp()
    log('backing up the world')
    make_backup(log, 'pre-update')
    old_mods = os.path.join(BACKUPS, 'mods_' + stamp)
    if os.path.isdir(os.path.join(ROOT, 'config')):
        shutil.copytree(os.path.join(ROOT, 'config'), os.path.join(BACKUPS, 'config_' + stamp))
    if os.path.isdir(mods_dir()):
        shutil.move(mods_dir(), old_mods)
    log('copying the new pack files')
    for item in os.listdir(src):
        if item in KEEP or re.match(r'neoforge-.*-installer\.jar', item):
            continue
        s, d = os.path.join(src, item), os.path.join(ROOT, item)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    new_bat, cur_bat = os.path.join(src, 'startserver.bat'), os.path.join(ROOT, 'startserver.bat')
    if os.path.isfile(new_bat) and os.path.isfile(cur_bat):
        nv = re.search(r'(?m)^set NEOFORGE_VERSION=(\S+)', open(new_bat, encoding='utf-8', errors='replace').read())
        txt = open(cur_bat, encoding='utf-8', errors='replace').read()
        cv = re.search(r'(?m)^set NEOFORGE_VERSION=(\S+)', txt)
        if nv and cv and nv.group(1) != cv.group(1):
            txt = re.sub(r'(?m)^set NEOFORGE_VERSION=[^\r\n]*', 'set NEOFORGE_VERSION=' + nv.group(1), txt)
            open(cur_bat, 'w', encoding='ascii', newline='').write(txt)
            shutil.rmtree(os.path.join(ROOT, 'libraries'), ignore_errors=True)
            log('NeoForge changed from %s to %s. It reinstalls on the next start.' % (cv.group(1), nv.group(1)))
    # bring back mods we added ourselves, unless the new pack already ships them
    _, pack_names = installed_state()
    restored = 0
    for x in get_extras():
        op = os.path.join(old_mods, x['filename'])
        if not os.path.isfile(op) or os.path.isfile(os.path.join(mods_dir(), x['filename'])):
            continue
        ids = {ckey(m['modId']) for m in scan_jar(op)['mods']}
        if ids and all(k in pack_names for k in ids):
            log('%s is now part of the pack, not restored' % x['filename'])
            continue
        shutil.copy2(op, os.path.join(mods_dir(), x['filename']))
        restored += 1
    log('restored %d added mod(s)' % restored)
    if not version:
        m = re.search(r'\d+(?:\.\d+)+', os.path.basename(zip_path))
        version = m.group(0) if m else None
    if version:
        open(VERSION_FILE, 'w').write(version)
    shutil.rmtree(tmp, ignore_errors=True)
    log('Update done. Old mods are in %s. Friends must update ATM10 to %s.' % (old_mods, version or 'the new version'))


def update_apply_job():
    def fn(log):
        chk = update_check()
        if not chk.get('ok'):
            raise ApiError('Add your CurseForge API key first.')
        if not chk['newer']:
            raise ApiError('The server is already up to date.')
        if not chk['serverPack']:
            raise ApiError('The author has not uploaded the server pack for %s yet.' % chk['latest'])
        u = cf_api('/v1/mods/%s/files/%s/download-url' % (chk['modId'], chk['serverPackFileId']))
        zp = os.path.join(BASE, '_server_pack_%s.zip' % chk['latest'])
        log('downloading the server pack')
        download(u['data'], zp, None, log)
        try:
            apply_pack(zp, chk['latest'], log)
        finally:
            try:
                os.remove(zp)
            except OSError:
                pass
    return run_job('Update ATM10', fn)


def update_from_zip_job(path):
    def fn(log):
        if not os.path.isfile(path):
            raise ApiError('Zip not found.')
        apply_pack(path, None, log)
    return run_job('Update from zip', fn)


# ---------------------------------------------------------------- players
NAME_RE = re.compile(r'^[A-Za-z0-9_]{3,16}$')


def uuid_for(name):
    try:
        d = http('https://api.mojang.com/users/profiles/minecraft/' + name, timeout=15)
        i = d['id']
        return '%s-%s-%s-%s-%s' % (i[:8], i[8:12], i[12:16], i[16:20], i[20:])
    except Exception:
        raise ApiError('Could not find a Minecraft account named %s.' % name)


def players_info():
    def rj(n):
        v = read_json(os.path.join(ROOT, n), [])
        return v if isinstance(v, list) else []
    return {'online': sorted(SRV.players), 'ops': rj('ops.json'), 'whitelist': rj('whitelist.json'),
            'banned': rj('banned-players.json'), 'whitelist_on': get_prop('white-list', 'false') == 'true'}


def player_action(action, name, reason=''):
    if not NAME_RE.match(name or ''):
        raise ApiError('That is not a valid Minecraft name.')
    live = SRV.running() and SRV.ready
    cmds = {'whitelist_add': 'whitelist add ' + name, 'whitelist_remove': 'whitelist remove ' + name, 'op': 'op ' + name,
            'deop': 'deop ' + name, 'kick': 'kick ' + name, 'ban': ('ban %s %s' % (name, reason)).strip(), 'pardon': 'pardon ' + name}
    if action not in cmds:
        raise ApiError('Unknown action.')
    if live:
        SRV.send(cmds[action])
        return {'via': 'server'}
    if action == 'kick':
        raise ApiError('The server must be running to kick a player.')
    if server_state() == 'external':
        raise ApiError('The server runs outside the dashboard. Stop it first.')
    files = {'whitelist_add': 'whitelist.json', 'whitelist_remove': 'whitelist.json', 'op': 'ops.json', 'deop': 'ops.json',
             'ban': 'banned-players.json', 'pardon': 'banned-players.json'}
    p = os.path.join(ROOT, files[action])
    data = read_json(p, [])
    data = [x for x in data if isinstance(x, dict)]
    if action in ('whitelist_remove', 'deop', 'pardon'):
        data = [x for x in data if x.get('name', '').lower() != name.lower()]
    else:
        if not any(x.get('name', '').lower() == name.lower() for x in data):
            u = uuid_for(name)
            if action == 'whitelist_add':
                data.append({'uuid': u, 'name': name})
            elif action == 'op':
                data.append({'uuid': u, 'name': name, 'level': 4, 'bypassesPlayerLimit': False})
            else:
                data.append({'uuid': u, 'name': name, 'created': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S +0000'),
                             'source': 'Dashboard', 'expires': 'forever', 'reason': reason or 'Banned by an operator.'})
    write_json(p, data)
    return {'via': 'file'}


# ---------------------------------------------------------------- files
ROOT_N = os.path.normpath(ROOT)
TEXT_EXT = {'.json', '.json5', '.txt', '.properties', '.toml', '.cfg', '.conf', '.yml', '.yaml', '.js', '.snbt', '.log', '.md',
            '.ini', '.xml', '.mcmeta', '.csv', '.bat', '.sh'}


def safe_path(rel, allow_root=True):
    rel = (rel or '').replace('/', os.sep).lstrip('\\/')
    p = os.path.normpath(os.path.join(ROOT, rel))
    if os.path.commonpath([p, ROOT_N]) != ROOT_N:
        raise ApiError('That path is outside the server folder.', 403)
    if p == ROOT_N and not allow_root:
        raise ApiError('Not allowed.', 403)
    return p


def files_list(rel):
    p = safe_path(rel)
    if not os.path.isdir(p):
        raise ApiError('Folder not found.', 404)
    items = []
    for f in os.listdir(p):
        fp = os.path.join(p, f)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        items.append({'name': f, 'dir': os.path.isdir(fp), 'size': None if os.path.isdir(fp) else st.st_size, 'time': st.st_mtime,
                      'text': os.path.splitext(f)[1].lower() in TEXT_EXT})
    items.sort(key=lambda x: (not x['dir'], x['name'].lower()))
    return {'path': os.path.relpath(p, ROOT).replace('\\', '/') if p != ROOT_N else '', 'items': items}


def file_read(rel):
    p = safe_path(rel, False)
    if not os.path.isfile(p):
        raise ApiError('File not found.', 404)
    if os.path.getsize(p) > 2 * 1024 * 1024:
        raise ApiError('That file is over 2 MB, too big to edit here. Download it instead.')
    if os.path.splitext(p)[1].lower() not in TEXT_EXT:
        raise ApiError('That file type is not editable here.')
    return {'text': open(p, 'r', encoding='utf-8', errors='replace').read()}


def file_write(rel, text):
    p = safe_path(rel, False)
    if not os.path.isfile(p) or os.path.splitext(p)[1].lower() not in TEXT_EXT:
        raise ApiError('Not an editable file.')
    bdir = os.path.join(BASE, '_file_backups')
    os.makedirs(bdir, exist_ok=True)
    shutil.copy2(p, os.path.join(bdir, '%s_%s' % (now_stamp(), os.path.basename(p))))
    with open(p, 'w', encoding='utf-8', newline='') as f:
        f.write(text)


def file_delete(rel):
    p = safe_path(rel, False)
    top = os.path.relpath(p, ROOT).replace('\\', '/')
    if top in ('world', 'mods', 'libraries', 'config', 'logs'):
        raise ApiError('Use the matching dashboard page for that folder.')
    if server_state() != 'stopped' and top.split('/')[0] in ('world', 'mods'):
        raise ApiError('Stop the server first.')
    os.makedirs(TRASH, exist_ok=True)
    shutil.move(p, os.path.join(TRASH, '%s_%s' % (now_stamp(), os.path.basename(p))))


# ---------------------------------------------------------------- config
PROP_INFO = {
    'motd': 'Server name shown in the multiplayer list', 'max-players': 'Most players at once', 'difficulty': 'peaceful, easy, normal or hard',
    'gamemode': 'survival, creative, adventure or spectator', 'pvp': 'Players can hurt each other', 'white-list': 'Only whitelisted players can join',
    'enforce-whitelist': 'Kick non-whitelisted players when the whitelist turns on', 'online-mode': 'Check that players own the game',
    'view-distance': 'Chunks sent to each player. Lower is faster', 'simulation-distance': 'Chunks that are simulated. Lower is faster',
    'allow-flight': 'Needed by many mods', 'spawn-protection': 'Radius around spawn that only ops can change',
    'enable-command-block': 'Allow command blocks', 'server-port': 'Game port (the router forwards this)', 'level-name': 'World folder name',
    'level-seed': 'World seed (new worlds only)', 'max-tick-time': 'Milliseconds before the watchdog stops a stuck server',
    'allow-nether': 'Allow the Nether', 'hardcore': 'One life per player', 'spawn-monsters': 'Spawn hostile mobs',
    'spawn-animals': 'Spawn animals', 'spawn-npcs': 'Spawn villagers', 'force-gamemode': 'Reset players to the default mode on join',
    'enable-rcon': 'Remote console', 'rcon.password': 'Remote console password', 'rcon.port': 'Remote console port',
    'player-idle-timeout': 'Minutes before idle players are kicked (0 = never)', 'op-permission-level': 'Default permission level for ops',
}


def config_get():
    props = [{'key': k, 'value': v, 'type': 'bool' if v in ('true', 'false') else ('number' if re.fullmatch(r'-?\d+', v or '') else 'text'),
              'info': PROP_INFO.get(k, '')} for k, v in read_props()]
    return {'props': props, 'xmx': read_xmx()[1] or '12G', 'running': server_state() != 'stopped'}


def config_save(props, xmx):
    p = os.path.join(ROOT, 'server.properties')
    lines = open(p, 'r', encoding='utf-8').read().split('\n') if os.path.isfile(p) else []
    seen = set()
    out = []
    for line in lines:
        s = line.rstrip('\r')
        if s and not s.startswith('#') and '=' in s:
            k = s.split('=', 1)[0]
            if k in props:
                out.append('%s=%s' % (k, props[k]))
                seen.add(k)
                continue
        out.append(s)
    while out and out[-1] == '':
        out.pop()
    for k, v in props.items():
        if k not in seen:
            if not re.fullmatch(r'[A-Za-z0-9_.\-]+', k):
                raise ApiError('Bad setting name: %s' % k)
            out.append('%s=%s' % (k, v))
    open(p, 'w', encoding='utf-8', newline='').write('\n'.join(out) + '\n')
    if xmx:
        if not re.fullmatch(r'\d{1,3}[GMgm]', xmx):
            raise ApiError('Memory must look like 12G.')
        jp = os.path.join(ROOT, 'user_jvm_args.txt')
        txt = open(jp, 'r', encoding='utf-8').read() if os.path.isfile(jp) else ''
        for flag in ('Xmx', 'Xms'):
            if re.search(r'(?m)^\s*-%s\S+' % flag, txt):
                txt = re.sub(r'(?m)^\s*-%s\S+' % flag, '-%s%s' % (flag, xmx.upper()), txt)
            else:
                txt = txt.rstrip('\r\n') + '\r\n-%s%s\r\n' % (flag, xmx.upper())
        open(jp, 'w', encoding='utf-8', newline='').write(txt)


# ---------------------------------------------------------------- settings + schedules
def settings():
    s = read_json(SETTINGS_FILE, {})
    s.setdefault('backup_keep', 10)
    return s


def schedules():
    s = read_json(SCHED_FILE, None)
    if s is None:
        s = [{'id': 'daily-backup', 'name': 'Daily backup', 'enabled': False, 'type': 'backup', 'mode': 'daily', 'time': '04:00', 'minutes': 0,
              'text': '', 'last': ''}]
        write_json(SCHED_FILE, s)
    return s


def restart_job(warn):
    def fn(log):
        if SRV.running() and SRV.ready:
            if warn > 0:
                SRV.send('say The server restarts in %d seconds.' % warn)
                time.sleep(warn)
            SRV.stop()
            log('stopping the server')
            SRV.wait_exit(240)
        if not SRV.running():
            SRV.start()
            log('server starting')
    return run_job('Restart', fn, needs_lock=False)


def run_schedule(item):
    t = item['type']
    if t == 'backup':
        def fn(log):
            make_backup(log, 'auto')
            prune_auto(int(settings().get('backup_keep', 10)))
        run_job('Scheduled backup', fn)
    elif t == 'restart':
        restart_job(30)
    elif t == 'command' and SRV.running() and SRV.ready:
        SRV.send(item.get('text', ''))
    elif t == 'announce' and SRV.running() and SRV.ready:
        SRV.send('say ' + item.get('text', ''))


FMT = '%Y-%m-%d %H:%M:%S'


def check_schedules(now=None):
    now = now or datetime.datetime.now()
    items = schedules()
    changed = False
    for it in items:
        if not it.get('enabled'):
            continue
        last = it.get('last') or ''
        due = False
        if it['mode'] == 'daily':
            hh, mm = (it.get('time') or '00:00').split(':')
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if not last:
                # a new schedule never catches up: if today's time has passed, wait for tomorrow
                it['last'] = (now if now >= target else now - datetime.timedelta(days=1)).strftime(FMT)
                changed = True
            else:
                due = now >= target and last[:10] != now.strftime('%Y-%m-%d')
        else:
            mins = max(1, int(it.get('minutes') or 60))
            if not last:
                it['last'] = now.strftime(FMT)
                changed = True
            else:
                due = (now - datetime.datetime.strptime(last, FMT)).total_seconds() >= mins * 60
        if due:
            it['last'] = now.strftime(FMT)
            changed = True
            run_schedule(it)
    if changed:
        write_json(SCHED_FILE, items)


def scheduler():
    while True:
        time.sleep(20)
        try:
            check_schedules()
        except Exception:
            with open(os.path.join(DATA, 'scheduler_error.log'), 'a', encoding='utf-8') as f:
                f.write('%s\n%s\n' % (datetime.datetime.now(), traceback.format_exc()))


# ---------------------------------------------------------------- API routing
def api(method, path, q, body, handler):
    def jbody():
        try:
            return json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            raise ApiError('Bad request body.')

    def one(name, default=''):
        return (q.get(name) or [default])[0]

    if path == '/api/status':
        return 200, status()
    if path == '/api/console':
        lines, nxt = SRV.since(int(one('since', '0')))
        return 200, {'lines': [t for _, t in lines], 'next': nxt}
    if path == '/api/start' and method == 'POST':
        SRV.start()
        return 200, {'ok': True}
    if path == '/api/stop' and method == 'POST':
        SRV.stop()
        return 200, {'ok': True}
    if path == '/api/kill' and method == 'POST':
        SRV.kill()
        return 200, {'ok': True}
    if path == '/api/restart' and method == 'POST':
        return 200, {'job': restart_job(int(jbody().get('warn', 30)))}
    if path == '/api/command' and method == 'POST':
        cmd = (jbody().get('cmd') or '').strip()
        if not cmd or '\n' in cmd:
            raise ApiError('Type a command first.')
        SRV.send(cmd)
        SRV.log('> ' + cmd)
        return 200, {'ok': True}
    if path == '/api/job':
        j = JOBS.get(one('id'))
        if not j:
            raise ApiError('Unknown task.', 404)
        return 200, {k: j[k] for k in ('id', 'title', 'log', 'done', 'ok')}
    # players
    if path == '/api/players':
        return 200, players_info()
    if path == '/api/players/action' and method == 'POST':
        b = jbody()
        return 200, player_action(b.get('action'), b.get('name'), b.get('reason', ''))
    # files
    if path == '/api/files':
        return 200, files_list(one('path'))
    if path == '/api/file' and method == 'GET':
        return 200, file_read(one('path'))
    if path == '/api/file' and method == 'POST':
        b = jbody()
        file_write(b.get('path'), b.get('text', ''))
        return 200, {'ok': True}
    if path == '/api/file/delete' and method == 'POST':
        file_delete(jbody().get('path'))
        return 200, {'ok': True}
    if path == '/api/file/download':
        p = safe_path(one('path'), False)
        if not os.path.isfile(p):
            raise ApiError('File not found.', 404)
        return 'file', p, os.path.basename(p)
    if path == '/api/file/upload' and method == 'POST':
        d = safe_path(one('path'))
        name = os.path.basename(one('name'))
        if not name or not os.path.isdir(d):
            raise ApiError('Bad upload target.')
        if server_state() != 'stopped' and os.path.relpath(d, ROOT).split(os.sep)[0] in ('world', 'mods'):
            raise ApiError('Stop the server first.')
        with open(os.path.join(d, name), 'wb') as f:
            f.write(body)
        return 200, {'ok': True}
    # mods
    if path == '/api/mods':
        return 200, {'mods': mods_list(), 'extras': len(get_extras())}
    if path == '/api/mods/search':
        return 200, mods_search(one('q'), int(one('offset', '0')))
    if path == '/api/mods/recommended':
        return 200, mods_recommended()
    if path == '/api/mods/install' and method == 'POST':
        b = jbody()
        ids = [i for i in b.get('ids', []) if re.fullmatch(r'[A-Za-z0-9]{8}', i)]
        if not ids:
            raise ApiError('Tick at least one mod.')
        return 200, {'job': mods_install_job(ids, bool(b.get('to_client', True)))}
    if path == '/api/mods/remove' and method == 'POST':
        if server_state() != 'stopped':
            raise ApiError('Stop the server first.')
        name = os.path.basename(jbody().get('file', ''))
        p = os.path.join(mods_dir(), name)
        if not os.path.isfile(p):
            raise ApiError('Mod file not found.', 404)
        dest = os.path.join(BASE, 'mods_removed')
        os.makedirs(dest, exist_ok=True)
        shutil.move(p, os.path.join(dest, name))
        write_json(EXTRAS_FILE, [x for x in get_extras() if x['filename'] != name])
        JAR_CACHE.pop(p, None)
        return 200, {'ok': True}
    if path == '/api/mods/upload' and method == 'POST':
        if server_state() != 'stopped':
            raise ApiError('Stop the server first.')
        name = os.path.basename(one('name'))
        if not name.lower().endswith('.jar'):
            raise ApiError('Only .jar files.')
        with open(os.path.join(mods_dir(), name), 'wb') as f:
            f.write(body)
        add_extra(name, None)
        return 200, {'ok': True}
    if path == '/api/share/build' and method == 'POST':
        return 200, {'job': share_job()}
    if path == '/api/share/download':
        p = os.path.join(SHARE, 'ATM10_Extra_Mods.zip')
        if not os.path.isfile(p):
            raise ApiError('Build the friends pack first.', 404)
        return 'file', p, 'ATM10_Extra_Mods.zip'
    # backups
    if path == '/api/backups':
        return 200, {'items': backups_list(), 'keep': settings()['backup_keep']}
    if path == '/api/backups/create' and method == 'POST':
        return 200, {'job': run_job('Backup world', lambda log: make_backup(log, 'world'))}
    if path == '/api/backups/restore' and method == 'POST':
        return 200, {'job': restore_job(jbody().get('name', ''))}
    if path == '/api/backups/delete' and method == 'POST':
        p = safe_backup(jbody().get('name', ''))
        os.makedirs(TRASH, exist_ok=True)
        shutil.move(p, os.path.join(TRASH, '%s_%s' % (now_stamp(), os.path.basename(p))))
        return 200, {'ok': True}
    if path == '/api/backups/download':
        p = safe_backup(one('name'))
        if not os.path.isfile(p):
            raise ApiError('Only zip backups can be downloaded.')
        return 'file', p, os.path.basename(p)
    # updates
    if path == '/api/update/status':
        return 200, {'has_key': bool(cf_key()), 'version': installed_version()}
    if path == '/api/update/check':
        try:
            return 200, update_check()
        except urllib.error.HTTPError as e:
            raise ApiError('CurseForge refused the request (%s). Check the API key.' % e.code)
    if path == '/api/update/apply' and method == 'POST':
        return 200, {'job': update_apply_job()}
    if path == '/api/update/zip' and method == 'POST':
        return 200, {'job': update_from_zip_job(jbody().get('path', ''))}
    if path == '/api/cfkey' and method == 'POST':
        k = (jbody().get('key') or '').strip()
        if not k:
            raise ApiError('Paste the key first.')
        open(KEY_FILE, 'w').write(k)
        return 200, {'ok': True}
    # config
    if path == '/api/config' and method == 'GET':
        return 200, config_get()
    if path == '/api/config' and method == 'POST':
        b = jbody()
        config_save({str(k): str(v) for k, v in (b.get('props') or {}).items()}, b.get('xmx', ''))
        return 200, {'ok': True}
    # schedules + settings
    if path == '/api/schedules' and method == 'GET':
        return 200, {'items': schedules(), 'keep': settings()['backup_keep']}
    if path == '/api/schedules/run' and method == 'POST':
        sid = jbody().get('id')
        item = next((x for x in schedules() if x['id'] == sid), None)
        if not item:
            raise ApiError('Save the schedule first, then run it.', 404)
        run_schedule(item)
        return 200, {'ok': True}
    if path == '/api/schedules' and method == 'POST':
        b = jbody()
        items = b.get('items', [])
        old_last = {x['id']: x.get('last', '') for x in schedules()}
        clean = []
        for it in items:
            if it.get('type') not in ('backup', 'restart', 'command', 'announce') or it.get('mode') not in ('daily', 'interval'):
                raise ApiError('Bad schedule.')
            clean.append({'id': it.get('id') or secrets.token_hex(3), 'name': str(it.get('name', ''))[:60], 'enabled': bool(it.get('enabled')),
                          'type': it['type'], 'mode': it['mode'], 'time': str(it.get('time') or '04:00'),
                          'minutes': int(it.get('minutes') or 0), 'text': str(it.get('text', ''))[:200], 'last': old_last.get(it.get('id'), '')})
        write_json(SCHED_FILE, clean)
        s = settings()
        s['backup_keep'] = max(1, int(b.get('keep', 10)))
        write_json(SETTINGS_FILE, s)
        return 200, {'ok': True}
    raise ApiError('Not found.', 404)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, ctype='application/json', raw=None):
        data = raw if raw is not None else json.dumps(obj).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')

    def _route(self, method):
        try:
            host = (self.headers.get('Host') or '').lower()
            if host not in ('localhost:%d' % PORT, '127.0.0.1:%d' % PORT):
                return self._send(403, {'error': 'Bad host.'})
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == '/':
                html = open(os.path.join(APPDIR, 'index.html'), 'r', encoding='utf-8').read().replace('%%TOKEN%%', TOKEN)
                return self._send(200, raw=html.encode('utf-8'), ctype='text/html; charset=utf-8')
            if not u.path.startswith('/api/'):
                return self._send(404, {'error': 'Not found.'})
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://localhost:%d' % PORT, 'http://127.0.0.1:%d' % PORT):
                return self._send(403, {'error': 'Bad origin.'})
            if (self.headers.get('X-Token') or (q.get('t') or [''])[0]) != TOKEN:
                return self._send(401, {'error': 'Reload the page.'})
            body = None
            if method == 'POST':
                n = int(self.headers.get('Content-Length') or 0)
                if n > 400 * 1024 * 1024:
                    return self._send(413, {'error': 'Too large.'})
                body = self.rfile.read(n) if n else b''
            res = api(method, u.path, q, body, self)
            if res[0] == 'file':
                size = os.path.getsize(res[1])
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.send_header('Content-Disposition', 'attachment; filename="%s"' % res[2].replace('"', ''))
                self.end_headers()
                with open(res[1], 'rb') as f:
                    shutil.copyfileobj(f, self.wfile)
                return
            return self._send(res[0], res[1])
        except ApiError as e:
            return self._send(e.code, {'error': str(e)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            return self._send(500, {'error': '%s: %s' % (type(e).__name__, e)})


def main():
    for d in (BACKUPS, SHARE, TRASH):
        os.makedirs(d, exist_ok=True)
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=tps_poller, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=lambda: installed_state(), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()

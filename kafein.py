#!/usr/bin/env python3
"""Kafein: bilgisayarın ekranını açık tutar. Windows + macOS öncelikli, Linux best-effort.
Tepsi simgesi, yerel web paneli (uzaktan kontrol), açılışta başlama ve gün/saat zamanlaması."""
import argparse, copy, ctypes, datetime, json, os, re, secrets, shutil, socket, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

WIN, MAC = sys.platform.startswith("win"), sys.platform == "darwin"
PLATFORM = "windows" if WIN else "macos" if MAC else "linux"
APP = "Kafein"

DEFAULTS = {
    "token": "", "port": 8765, "host": "0.0.0.0",
    "default_minutes": 180,      # açılışta / tepsiden başlatınca süre; 0 = süresiz
    "start_on_launch": True,     # uygulama açılınca hemen uyanık tut
    "jiggle": False, "jiggle_idle": 300,
    "min_battery": 10,           # şarjda değilken bu %'nin altında bitir; 0 = kapalı
    "autostart": False,
    "schedule": {"enabled": False, "days": [0, 1, 2, 3, 4], "start": "09:00", "end": "18:00"},  # 0 = Pazartesi
}


# ---------------- ayar dosyası ----------------
def config_path():
    if WIN: base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif MAC: base = os.path.expanduser("~/Library/Application Support")
    else: base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = os.path.join(base, APP); os.makedirs(d, exist_ok=True)
    return os.path.join(d, "config.json")


class Config:
    def __init__(self):
        self.path, self.lock = config_path(), threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        try:
            with open(self.path) as f: self._merge(self.data, json.load(f))
        except (OSError, ValueError): pass
        if not self.data["token"]:
            self.data["token"] = secrets.token_urlsafe(8); self.save()

    @staticmethod
    def _merge(dst, src):
        for k, v in src.items():
            if k in dst and isinstance(dst[k], dict) and isinstance(v, dict): Config._merge(dst[k], v)
            elif k in dst: dst[k] = v

    def update(self, patch):
        with self.lock:
            patch = {k: v for k, v in patch.items() if k not in ("token", "port", "host")}
            self._merge(self.data, patch); self.save()

    def save(self):
        try:
            with open(self.path, "w") as f: json.dump(self.data, f, indent=2)
        except OSError: pass

    def __getitem__(self, k): return self.data[k]


# ---------------- açılışta başlatma ----------------
def launch_cmd():
    base = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, os.path.abspath(__file__)]
    return base + ["--background"]


def _autostart_file():
    if MAC: return os.path.expanduser("~/Library/LaunchAgents/com.kafein.app.plist")
    return os.path.expanduser("~/.config/autostart/kafein.desktop")


def get_autostart():
    if WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, APP); return True
        except OSError: return False
    return os.path.exists(_autostart_file())


def set_autostart(on):
    cmd = launch_cmd()
    if WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
            if on: winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try: winreg.DeleteValue(k, APP)
                except OSError: pass
        return
    p = _autostart_file()
    if not on:
        if os.path.exists(p): os.remove(p)
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if MAC:
        args = "".join(f"<string>{a}</string>" for a in cmd)
        body = ('<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
                '<key>Label</key><string>com.kafein.app</string>'
                f'<key>ProgramArguments</key><array>{args}</array><key>RunAtLoad</key><true/></dict></plist>')
    else:
        body = f"[Desktop Entry]\nType=Application\nName={APP}\nExec={' '.join(cmd)}\nX-GNOME-Autostart-enabled=true\n"
    with open(p, "w") as f: f.write(body)


# ---------------- uyanık tutma çekirdeği ----------------
def _hm(s):
    h, m = s.split(":"); return int(h) * 60 + int(m)


class Awake:
    def __init__(self, cfg):
        self.cfg, self.lock = cfg, threading.RLock()
        self.end, self.active, self.proc = None, False, None
        self.block_until = None   # elle durdurulan zamanlanmış pencerenin bitişi (tekrar başlamasın)
        self.stop_evt = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _acquire(self):
        if self.active: return
        if WIN: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x2 | 0x1)
        elif MAC: self.proc = subprocess.Popen(["caffeinate", "-d", "-i"])
        elif shutil.which("systemd-inhibit"):
            self.proc = subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--who=kafein", "--why=user", "sleep", "infinity"])
        self.active = True

    def _release(self):
        if not self.active: return
        if WIN: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        if self.proc: self.proc.terminate(); self.proc = None
        self.active = False
        w = self.window()
        if w: self.block_until = w[1]   # pencere içinde bitirildi: pencere bitene dek yeniden başlatma

    def window(self, now=None):
        """Şu an zamanlama penceresindeyse (başlangıç_dt, bitiş_dt), değilse None."""
        s = self.cfg["schedule"]
        if not s["enabled"]: return None
        now = now or datetime.datetime.now()
        if now.weekday() not in s["days"]: return None
        a, b, cur = _hm(s["start"]), _hm(s["end"]), now.hour * 60 + now.minute
        if not a <= cur < b: return None
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day + datetime.timedelta(minutes=a), day + datetime.timedelta(minutes=b)

    def _nudge(self):
        try:
            if WIN:
                import ctypes.wintypes as w
                u, pt = ctypes.windll.user32, w.POINT()
                u.GetCursorPos(ctypes.byref(pt)); u.SetCursorPos(pt.x + 1, pt.y); time.sleep(0.05); u.SetCursorPos(pt.x, pt.y)
            elif MAC:
                cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
                class P(ctypes.Structure): _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
                cg.CGEventCreate.restype = ctypes.c_void_p
                cg.CGEventGetLocation.restype = P; cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
                cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
                cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, P, ctypes.c_uint32]
                cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
                p = cg.CGEventGetLocation(cg.CGEventCreate(None))
                for dx in (1, 0):
                    cg.CGEventPost(0, cg.CGEventCreateMouseEvent(None, 5, P(p.x + dx, p.y), 0)); time.sleep(0.05)
            elif shutil.which("xdotool"):
                subprocess.run(["xdotool", "mousemove_relative", "1", "0"]); subprocess.run(["xdotool", "mousemove_relative", "--", "-1", "0"])
        except Exception as e: print("fare hatası:", e, file=sys.stderr)

    def _idle(self):
        try:
            if WIN:
                import ctypes.wintypes as w
                class LII(ctypes.Structure): _fields_ = [("cb", w.UINT), ("t", w.DWORD)]
                i = LII(); i.cb = ctypes.sizeof(i); ctypes.windll.user32.GetLastInputInfo(ctypes.byref(i))
                return (ctypes.windll.kernel32.GetTickCount() - i.t) / 1000
            if MAC:
                cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
                cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
                cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int, ctypes.c_uint32]
                return cg.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF)
            if shutil.which("xprintidle"): return int(subprocess.check_output(["xprintidle"])) / 1000
        except Exception: pass
        return 1e9

    def _battery(self):
        try:
            if WIN:
                import ctypes.wintypes as w
                class S(ctypes.Structure): _fields_ = [("ac", w.BYTE), ("flag", w.BYTE), ("pct", w.BYTE), ("x", w.BYTE), ("a", w.DWORD), ("b", w.DWORD)]
                p = S(); ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p))
                return None if p.pct == 255 else (p.pct, p.ac == 1)
            if MAC:
                o = subprocess.check_output(["pmset", "-g", "batt"], text=True)
                if "InternalBattery" not in o: return None
                return int(re.search(r"(\d+)%", o).group(1)), "AC Power" in o
        except Exception: pass
        return None

    def _loop(self):
        last_batt = 0
        while not self.stop_evt.wait(1):
            with self.lock:
                now = datetime.datetime.now()
                if self.active and self.end is not None and time.time() >= self.end: self._release(); self.end = None
                w = self.window(now)
                if self.block_until and now >= self.block_until: self.block_until = None
                if w and not self.active and self.block_until != w[1]:        # zamanlanmış başlangıç
                    self.end = w[1].timestamp(); self._acquire()
                if self.active and time.time() - last_batt >= 10:
                    last_batt = time.time(); b, mb = self._battery(), self.cfg["min_battery"]
                    if mb and b and not b[1] and b[0] < mb:
                        print(f"Pil %{b[0]} < %{mb}: oturum bitti"); self._release(); self.end = None
                if self.active and self.cfg["jiggle"] and self._idle() >= self.cfg["jiggle_idle"]: self._nudge()

    # --- dış kontrol ---
    def set_minutes(self, m):
        with self.lock:
            self.end = None if m <= 0 else time.time() + m * 60
            self.block_until = None; self._acquire()

    def extend(self, m):
        with self.lock:
            self.end = (self.end if self.end and self.active else time.time()) + m * 60
            self.block_until = None; self._acquire()

    def stop(self):
        with self.lock: self._release(); self.end = None

    def shutdown(self):
        self.stop_evt.set()
        with self.lock: self._release()

    def status(self):
        with self.lock:
            rem = None if self.end is None else max(0, int(self.end - time.time()))
            return {"active": self.active, "remaining_seconds": rem, "indefinite": self.active and self.end is None,
                    "in_schedule_window": self.window() is not None, "platform": PLATFORM}


# ---------------- web paneli + API ----------------
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Kafein</title>
<style>body{font-family:system-ui;max-width:460px;margin:1.5rem auto;padding:0 1rem;color:#222}
button{padding:.6rem .9rem;margin:.15rem;font-size:1rem}#t{font-size:2.6rem;font-weight:600}
fieldset{margin:1rem 0;border:1px solid #ccc;border-radius:8px}label{display:block;margin:.35rem 0}.d{display:inline-block;margin-right:.6rem}
input[type=time],input[type=number]{font-size:1rem}</style>
<h2>☕ Kafein</h2><div id=t>…</div><div id=s></div>
<div><button onclick="p('extend',15)">+15 dk</button><button onclick="p('extend',60)">+1 sa</button>
<button onclick="p('set',0)">Süresiz</button><button onclick="p('set',cfg.default_minutes)">Başlat</button><button onclick="p('stop')">Durdur</button></div>
<fieldset><legend>Ayarlar</legend>
<label><input type=checkbox id=autostart> Bilgisayar açılınca otomatik çalış</label>
<label><input type=checkbox id=start_on_launch> Uygulama açılınca hemen uyanık tut</label>
<label>Varsayılan süre (dk, 0 = süresiz) <input type=number id=default_minutes min=0 style="width:5em"></label>
<label><input type=checkbox id=jiggle> Boşta kalınca fareyi oynat</label>
<label>Boşta süre (sn) <input type=number id=jiggle_idle min=5 style="width:5em"></label>
<label>Pil bu %'nin altına inince bitir (0 = kapalı) <input type=number id=min_battery min=0 max=100 style="width:4em"></label></fieldset>
<fieldset><legend>Zamanlama</legend><label><input type=checkbox id=sch_enabled> Seçili gün ve saatlerde otomatik başlat</label>
<div id=days></div><label>Başlangıç <input type=time id=sch_start> Bitiş <input type=time id=sch_end></label>
<button onclick="setDays([0,1,2,3,4])">Hafta içi</button><button onclick="setDays([0,1,2,3,4,5,6])">Her gün</button></fieldset>
<button onclick="save()"><b>Kaydet</b></button> <span id=ok></span> <button onclick="if(confirm('Kafein kapansın mı?'))p('quit')" style="float:right">Çık</button>
<script>const k=new URLSearchParams(location.search).get('token')||'',h={'X-Token':k,'Content-Type':'application/json'};
const G=id=>document.getElementById(id),DN=['Pzt','Sal','Çar','Per','Cum','Cmt','Paz'];let st={},cfg={default_minutes:180};
G('days').innerHTML=DN.map((n,i)=>`<label class=d><input type=checkbox class=dy value=${i}> ${n}</label>`).join('');
function setDays(a){document.querySelectorAll('.dy').forEach(c=>c.checked=a.includes(+c.value))}
async function f(u,o){return (await fetch(u,Object.assign({headers:h},o))).json()}
async function r(){try{st=await f('/api/status');let s=st.remaining_seconds;
G('t').textContent=!st.active?'Kapalı':s==null?'Süresiz':new Date(s*1000).toISOString().substr(11,8);
G('s').textContent=st.platform+(st.in_schedule_window?' · zamanlama penceresinde':'')}catch(e){G('t').textContent='Bağlantı yok'}}
async function p(a,m){await f('/api/'+a,{method:'POST',body:JSON.stringify({minutes:m})});r()}
async function load(){cfg=await f('/api/config');for(const x of ['autostart','start_on_launch','jiggle'])G(x).checked=cfg[x];
for(const x of ['default_minutes','jiggle_idle','min_battery'])G(x).value=cfg[x];
G('sch_enabled').checked=cfg.schedule.enabled;G('sch_start').value=cfg.schedule.start;G('sch_end').value=cfg.schedule.end;setDays(cfg.schedule.days)}
async function save(){const b={schedule:{enabled:G('sch_enabled').checked,start:G('sch_start').value,end:G('sch_end').value,
days:[...document.querySelectorAll('.dy')].filter(c=>c.checked).map(c=>+c.value)}};
for(const x of ['autostart','start_on_launch','jiggle'])b[x]=G(x).checked;
for(const x of ['default_minutes','jiggle_idle','min_battery'])b[x]=+G(x).value;
cfg=await f('/api/config',{method:'POST',body:JSON.stringify(b)});G('ok').textContent='Kaydedildi ✓';setTimeout(()=>G('ok').textContent='',2000)}
load();r();setInterval(r,1000)</script>"""


def full_config(cfg):
    d = copy.deepcopy(cfg.data); d.pop("token", None); d["autostart"] = get_autostart(); return d


def make_handler(app, cfg, on_quit):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _ok(self):
            q = parse_qs(urlparse(self.path).query)
            return secrets.compare_digest(self.headers.get("X-Token") or q.get("token", [""])[0], cfg["token"])

        def _send(self, code, body, ctype="application/json"):
            b = (body if ctype != "application/json" else json.dumps(body)).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            path = urlparse(self.path).path
            if not self._ok(): return self._send(401, {"error": "unauthorized"})
            if path == "/": return self._send(200, PAGE, "text/html; charset=utf-8")
            if path == "/api/status": return self._send(200, app.status())
            if path == "/api/config": return self._send(200, full_config(cfg))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._ok(): return self._send(401, {"error": "unauthorized"})
            try: d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError: return self._send(400, {"error": "bad json"})
            path = urlparse(self.path).path
            try:
                if path == "/api/extend": app.extend(float(d.get("minutes", 15)))
                elif path == "/api/set": app.set_minutes(float(d.get("minutes", 0)))
                elif path == "/api/jiggle": cfg.update({"jiggle": bool(d.get("enabled"))}); return self._send(200, full_config(cfg))
                elif path == "/api/stop": app.stop()
                elif path == "/api/config":
                    if "autostart" in d: set_autostart(bool(d["autostart"]))
                    cfg.update({k: v for k, v in d.items() if k != "autostart"}); return self._send(200, full_config(cfg))
                elif path == "/api/quit": self._send(200, {"ok": True}); on_quit(); return
                else: return self._send(404, {"error": "not found"})
            except (ValueError, TypeError, KeyError, OSError) as e: return self._send(400, {"error": str(e)})
            self._send(200, app.status())
    return H


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except OSError: return "127.0.0.1"


# ---------------- tepsi simgesi ----------------
def make_tray(app, cfg, panel_url, on_quit):
    import pystray
    from PIL import Image, ImageDraw
    def icon_img(on):
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0)); d = ImageDraw.Draw(im)
        col = (230, 90, 30, 255) if on else (130, 130, 130, 255)
        d.rounded_rectangle((10, 22, 46, 54), 6, fill=col); d.arc((38, 28, 58, 46), 270, 90, fill=col, width=5)
        for x in (20, 30): d.line((x, 8, x + 4, 18), fill=col, width=3)
        return im
    def label(_=None):
        s = app.status()
        return "Kapalı" if not s["active"] else "Süresiz" if s["remaining_seconds"] is None else f"Kalan: {s['remaining_seconds'] // 60} dk"
    icon = pystray.Icon(APP, icon_img(False), APP, pystray.Menu(
        pystray.MenuItem(label, None, enabled=False),
        pystray.MenuItem("Paneli aç", lambda *_: webbrowser.open(panel_url), default=True),
        pystray.MenuItem("Başlat", lambda *_: app.set_minutes(cfg["default_minutes"])),
        pystray.MenuItem("+15 dk", lambda *_: app.extend(15)),
        pystray.MenuItem("Süresiz", lambda *_: app.set_minutes(0)),
        pystray.MenuItem("Durdur", lambda *_: app.stop()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Çık", lambda *_: on_quit())))
    def refresh():
        last = None
        while not app.stop_evt.wait(1):
            a = app.active
            if a != last: icon.icon = icon_img(a); last = a
            try: icon.update_menu()
            except Exception: pass
    threading.Thread(target=refresh, daemon=True).start()
    return icon


def main():
    ap = argparse.ArgumentParser(description="Kafein: ekranı açık tutar")
    ap.add_argument("--background", action="store_true", help="paneli otomatik açma (açılışta başlatma bunu kullanır)")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--minutes", type=float, help="bu açılışta süre (dk); 0 = süresiz")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    cfg = Config()
    port = a.port or cfg["port"]
    local = f"http://127.0.0.1:{port}/?token={cfg['token']}"
    try: srv = ThreadingHTTPServer((cfg["host"], port), BaseHTTPRequestHandler)
    except OSError:                                    # port dolu: zaten çalışıyor
        if not a.background: webbrowser.open(local)
        print("Kafein zaten çalışıyor."); return
    app, quit_evt, tray = Awake(cfg), threading.Event(), []
    def on_quit():
        quit_evt.set(); threading.Thread(target=srv.shutdown, daemon=True).start()
        for t in tray: t.stop()
    srv.RequestHandlerClass = make_handler(app, cfg, on_quit)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"{APP} [{PLATFORM}] çalışıyor. Panel: {local}\nUzaktan:  http://{lan_ip()}:{port}/?token={cfg['token']}")
    if a.minutes is not None: app.set_minutes(a.minutes)
    elif cfg["start_on_launch"] and not app.window(): app.set_minutes(cfg["default_minutes"])
    if not a.background: webbrowser.open(local)
    if not a.no_tray:
        try: tray.append(make_tray(app, cfg, local, on_quit))
        except Exception as e: print("Tepsi simgesi kullanılamıyor:", e)
    try:
        if tray: tray[0].run()       # ana iş parçacığı (macOS gerektirir)
        else: quit_evt.wait()
    except KeyboardInterrupt: pass
    finally: app.shutdown()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Persistent dialog history for the dashboard (SQLite, WAL).

Dialogs used to be parsed from `docker logs` on every request, so recreating the quasar
container wiped the history. Everything now lands in dialogs.sqlite3 first:

  raw_log  every container/journal line (deduped by (docker timestamp, line hash))
  dialogs  projection built incrementally from raw_log lines (voice/text) plus brain
           turns from the claude router (prompt/answer), so history survives restarts
  meta / cursors / brain_seen  ingest bookkeeping

An Ingester thread follows `docker logs -t -f` of every container named like the quasar
(legacy `aisis-yandex-quasar-*`, blue/green `aisis-quasar-prod-*`, incl. stopped ones) and
re-attaches by name after restart/recreate; the dashboard only reads from SQLite.

`python dialogstore.py stats|rebuild` for maintenance.
"""
import calendar, hashlib, json, os, re, sqlite3, subprocess, sys, threading, time
from datetime import datetime, timedelta

DB_PATH = os.environ.get("AISIS_DIALOGS_DB", os.path.expanduser("~/.local/share/aisis/dialogs.sqlite3"))
ROUTER_DB = os.environ.get("AISIS_ROUTER_DB", os.path.expanduser("~/.local/state/aisis/claude/router.sqlite3"))
ADAPTER_DB = os.environ.get("AISIS_STATE_DB", os.path.expanduser("~/.local/state/aisis/adapter.sqlite3"))
ADAPTER_UNIT = "aisis-station-adapter"
DEFAULT_STATION = "M00ZRW3008RAJG"
RETENTION_DAYS = 180
STALE_RUNNING_S = 600          # dialog without "ended" older than this is shown as interrupted
BRAIN_ATTACH_WINDOW_S = 180    # router turn is attached to a voice dialog started within this window
VOICE_LEAD_S = 5               # a speaker score may precede its dialog's "started" log line by this much
LIVE_LOG_S = 30                # on_log hooks only see lines this fresh (a restart replays history via --since)
# prod/legacy quasar containers (any compose prefix like `<hash>_`); stage containers are not dialogs
CONTAINER_RE = re.compile(r"^(?:[0-9a-f]{12}_)?aisis-(?:yandex-)?quasar(?!-stage)")

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+(\w+)\s+(\S+)\s+(.*)$")
DTS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z$")
JOURNAL = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}) (.*)$")
USER_TURN = re.compile(r"(?:^|\n)Пользователь: ", re.M)

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_log(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, dts TEXT NOT NULL, h TEXT NOT NULL,
  src TEXT NOT NULL, line TEXT NOT NULL, UNIQUE(dts, h));
CREATE INDEX IF NOT EXISTS raw_log_ts ON raw_log(ts);
CREATE INDEX IF NOT EXISTS raw_log_src_dts ON raw_log(src, dts);
CREATE TABLE IF NOT EXISTS dialogs(
  id TEXT PRIMARY KEY, station TEXT, kind TEXT NOT NULL, src TEXT,
  ts_start REAL NOT NULL, ts_end REAL, start_full TEXT, end_full TEXT,
  status TEXT NOT NULL DEFAULT '', heard TEXT NOT NULL DEFAULT '[]',
  answered TEXT NOT NULL DEFAULT '[]', actions TEXT NOT NULL DEFAULT '[]',
  errors TEXT NOT NULL DEFAULT '', brain TEXT NOT NULL DEFAULT '[]', raw TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS dialogs_ts ON dialogs(ts_start);
CREATE INDEX IF NOT EXISTS dialogs_src_ts ON dialogs(src, ts_start);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS cursors(cid TEXT PRIMARY KEY, dts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS brain_seen(id TEXT PRIMARY KEY);
-- one row per utterance/reply/event of a dialog with the exact log timestamp (chat transcript order)
CREATE TABLE IF NOT EXISTS dialog_turns(
  id INTEGER PRIMARY KEY, dialog_id TEXT NOT NULL, ts REAL NOT NULL, role TEXT NOT NULL,
  kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', res TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS dialog_turns_dialog ON dialog_turns(dialog_id, ts);
-- written by stt_wyoming.py via speakerid.record_score; same definition lives there
CREATE TABLE IF NOT EXISTS voice_scores(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, dur REAL, text TEXT, score REAL, best TEXT,
  verdict TEXT, mode TEXT, ms REAL, model TEXT);
CREATE INDEX IF NOT EXISTS voice_scores_ts ON voice_scores(ts);
"""


def to_ts(ts):
    # container logs are UTC; strptime+timegm keeps the epoch right regardless of host TZ
    return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")) + int(ts[20:23] or 0) / 1000


def dts_epoch(dts):
    m = DTS.match(dts)
    if not m:
        return None
    return calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")) + float("0." + (m.group(2) or "0"))


def loc_hms(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def utc_full(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t)) + ".%03d" % int((t % 1) * 1000)


def line_hash(line):
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:16]


def user_text(prompt):
    """Router prompts carry persona/memory preambles; the utterance follows the last 'Пользователь: '."""
    parts = USER_TURN.split(prompt)
    return (parts[-1] if len(parts) > 1 else prompt).strip()


def connect(path=DB_PATH):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


# ---------- log line -> chat turns ----------
FILLER_RE = re.compile(r"Filler '(.*)' \(\d+b\) sent$")
ASR_CORR_RE = re.compile(r"ASR (corrected|correction to confirm|correction ignored) '(.+?)' -> '(.+?)' \((.*)\)$")
ASR_KIND = {"corrected": "коррекция ASR", "correction to confirm": "коррекция: переспрос", "correction ignored": "коррекция отклонена"}


def line_turns(lvl, msg, src="", dialog_open=True):
    """(role, kind, text, res) tuples for one quasar log message. role: user (heard / typed), assistant
    (spoken reply, filler) or event (fast intent, action, ASR correction, error). `dialog_open` is False
    after the dialog's "ended" line, when stray warnings are not that dialog's failure."""
    out = []
    m = re.match(r"Heard: (.*)", msg)
    if m and m.group(1).strip():
        out.append(("user", "speech", m.group(1).strip(), ""))
    m = re.match(r"Fast intent for text '(.+?)': (.*)$", msg)
    if m:
        out.append(("user", "text", m.group(1), ""))
        out.append(("event", "intent", m.group(1), m.group(2)))
    else:
        m = re.match(r"Fast intent for '(.+?)': (.*)$", msg)
        if m:
            out.append(("event", "intent", m.group(1), m.group(2)))
    m = ASR_CORR_RE.match(msg)
    if m:
        out.append(("event", "asr", m.group(2), f"{m.group(3)} ({m.group(4)}) · {ASR_KIND[m.group(1)]}"))
    m = re.match(r"Fast intent executed: (.+)$", msg)
    if m:
        out.append(("event", "action", m.group(1), ""))
    m = FILLER_RE.match(msg)
    if m:
        out.append(("assistant", "filler", m.group(1), ""))
    m = re.match(r"Synthesizing: (.*)", msg)
    if m and m.group(1).strip():
        out.append(("assistant", "speech", m.group(1), ""))
    if lvl in ("ERROR", "WARN") and dialog_open:
        out.append(("event", "error", f"{src.split('/')[-1]}: {msg}"[:200], ""))
    return out


def add_turns(conn, dialog_id, t, turns):
    for i, (role, kind, text, res) in enumerate(turns):
        conn.execute("INSERT INTO dialog_turns(dialog_id,ts,role,kind,text,res) VALUES(?,?,?,?,?,?)",
                     (dialog_id, t + i * 1e-4, role, kind, text, res))


def backfill_turns(conn):
    """One-off: derive turns for docker dialogs that have none by replaying raw_log (idempotent, resumable)."""
    if conn.execute("SELECT v FROM meta WHERE k='turns_backfill_v1'").fetchone():
        return 0
    have = {r[0] for r in conn.execute("SELECT DISTINCT dialog_id FROM dialog_turns")}
    n = 0
    for (src,) in conn.execute("SELECT DISTINCT src FROM raw_log WHERE src LIKE 'docker:%'").fetchall():
        cur, ended = None, set()
        for ts, line in conn.execute("SELECT ts, line FROM raw_log WHERE src=? ORDER BY ts, id", (src,)).fetchall():
            m = LINE.match(line)
            if not m:
                continue
            _, lvl, lsrc, msg = m.groups()
            m2 = re.match(r"Dialog ([0-9a-f-]{36}) (started|ended)", msg)
            if m2:
                if m2.group(2) == "started":
                    cur = m2.group(1)
                else:
                    ended.add(m2.group(1))
                continue
            if cur is None or cur in have:
                continue
            turns = line_turns(lvl, msg, lsrc, cur not in ended)
            if turns:
                add_turns(conn, cur, ts, turns)
                n += len(turns)
    conn.execute("INSERT INTO meta(k,v) VALUES('turns_backfill_v1','1') ON CONFLICT(k) DO UPDATE SET v=excluded.v")
    conn.commit()
    return n


# ---------- log line -> dialog projection ----------
class DialogParser:
    """Stateful per-source parser; events attach to the most recently started dialog."""

    def __init__(self, conn, src, station=DEFAULT_STATION):
        self.src, self.station = src, station
        self.cache = {}
        r = conn.execute("SELECT id FROM dialogs WHERE src=? ORDER BY ts_start DESC LIMIT 1", (src,)).fetchone()
        self.cur = r["id"] if r else None

    def _get(self, conn, did):
        d = self.cache.get(did)
        if d is None:
            r = conn.execute("SELECT * FROM dialogs WHERE id=?", (did,)).fetchone()
            if r is None:
                return None
            d = {"id": did, "t": r["ts_start"], "tend": r["ts_end"], "start": r["start_full"], "end": r["end_full"] or "",
                 "status": r["status"], "err": r["errors"], "heard": json.loads(r["heard"]),
                 "said": json.loads(r["answered"]), "reqs": json.loads(r["actions"]),
                 "raw": json.loads(r["raw"]), "textreq": r["kind"] == "text"}
            self._remember(did, d)
        return d

    def _remember(self, did, d):
        if len(self.cache) > 64:
            self.cache.pop(next(iter(self.cache)))
        self.cache[did] = d

    def _save(self, conn, d):
        kind = "text" if (not d["heard"] and d["textreq"]) else "voice"
        conn.execute(
            """INSERT INTO dialogs(id,station,kind,src,ts_start,ts_end,start_full,end_full,status,heard,answered,actions,errors,raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, ts_end=excluded.ts_end, end_full=excluded.end_full,
                 status=excluded.status, heard=excluded.heard, answered=excluded.answered, actions=excluded.actions,
                 errors=excluded.errors, raw=excluded.raw""",
            (d["id"], self.station, kind, self.src, d["t"], d["tend"], d["start"], d["end"] or None, d["status"],
             json.dumps(d["heard"], ensure_ascii=False), json.dumps(d["said"], ensure_ascii=False),
             json.dumps(d["reqs"], ensure_ascii=False), d["err"], json.dumps(d["raw"][-200:], ensure_ascii=False)))

    def feed(self, conn, line):
        m = LINE.match(line)
        if not m:
            return
        ts, lvl, src, msg = m.groups()
        try:
            t = to_ts(ts)
        except ValueError:
            return
        hms = loc_hms(t)
        m = re.match(r"Dialog ([0-9a-f-]{36}) started", msg)
        if m:
            did = m.group(1)
            d = self._get(conn, did) or {"id": did, "t": t, "tend": None, "start": ts, "end": "", "status": "",
                                         "err": "", "heard": [], "said": [], "reqs": [], "raw": [], "textreq": False}
            self._remember(did, d)
            self.cur = did
            self._save(conn, d)
            return
        m = re.match(r"Dialog ([0-9a-f-]{36}) ended: (\S+)", msg)
        if m:
            d = self._get(conn, m.group(1))
            if d is not None:
                d["end"], d["tend"], d["status"] = ts, t, m.group(2)
                d["raw"].append(f"{hms} [{lvl}] {msg}")
                self._save(conn, d)
            return
        d = self._get(conn, self.cur) if self.cur else None
        if d is None:
            return
        add_turns(conn, d["id"], t, line_turns(lvl, msg, src, not d["status"]))
        if lvl in ("ERROR", "WARN") and not d["err"] and not d["status"]:   # lines after "ended" are not that dialog's failure
            d["err"] = f"{src.split('/')[-1]}: {msg}"[:200]
        hit = None
        m = re.match(r"Fast intent for text '(.+?)': (.*)$", msg)
        if m:
            d["textreq"] = True
        m = m or re.match(r"Fast intent for '(.+?)': (.*)$", msg)
        if m:
            hit = {"ts": hms, "kind": "запрос", "text": m.group(1), "res": m.group(2)}
            d["reqs"].append(hit)
        m = re.match(r"ASR (corrected|correction to confirm|correction ignored) '(.+?)' -> '(.+?)' \((.*)\)$", msg)
        if m:   # raw ASR text vs the corrector's guess; the raw text itself stays in `heard`
            hit = {"ts": hms, "kind": {"corrected": "коррекция ASR", "correction to confirm": "коррекция: переспрос"}.get(m.group(1), "коррекция отклонена"),
                   "text": m.group(2), "res": f"{m.group(3)} ({m.group(4)})"}
            d["reqs"].append(hit)
        m = re.match(r"Fast intent executed: (.+)$", msg)
        if m:
            hit = {"ts": hms, "kind": "действие", "text": m.group(1), "res": ""}
            d["reqs"].append(hit)
        m = re.match(r"Heard: (.*)", msg)
        if m and m.group(1).strip():
            d["heard"].append(m.group(1))
        m = re.match(r"Synthesizing: (.*)", msg)
        if m and m.group(1).strip():
            d["said"].append(m.group(1))
        if hit is not None or lvl in ("ERROR", "WARN") or "Dialog" not in msg:
            d["raw"].append(f"{hms} [{lvl}] {msg[:300]}")
        self._save(conn, d)


def store_line(conn, parser, dts, src, line):
    """Insert one log line; feed the parser only if it is new. Returns True when inserted."""
    t = dts_epoch(dts)
    if t is None:
        return False
    line = ANSI.sub("", line.rstrip("\r\n"))
    cur = conn.execute("INSERT OR IGNORE INTO raw_log(ts,dts,h,src,line) VALUES(?,?,?,?,?)",
                       (t, dts, line_hash(line), src, line))
    if cur.rowcount and parser is not None:
        parser.feed(conn, line)
    return bool(cur.rowcount)


def import_lines(conn, src, items, station=DEFAULT_STATION):
    """Idempotent bulk import of (dts, line) pairs, e.g. archived log excerpts."""
    parser = DialogParser(conn, src, station)
    n = 0
    for dts, line in sorted(items):
        n += store_line(conn, parser, dts, src, line)
    conn.commit()
    return n


# ---------- brain turns (claude router context) and adapter turns ----------
def _attach_or_insert_brain(conn, bid, station, created, prompt, answer):
    text = user_text(prompt)
    turn = {"prompt": text, "answer": answer, "ts": created}
    r = conn.execute(
        """SELECT id, brain FROM dialogs WHERE src LIKE 'docker:%' AND kind IN ('voice','text') AND station=?
           AND ts_start BETWEEN ? AND ? AND brain='[]' ORDER BY ts_start DESC LIMIT 1""",
        (station, created - BRAIN_ATTACH_WINDOW_S, created + 5)).fetchone()
    if r:
        conn.execute("UPDATE dialogs SET brain=? WHERE id=?", (json.dumps([turn], ensure_ascii=False), r["id"]))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO dialogs(id,station,kind,src,ts_start,ts_end,start_full,end_full,status,heard,answered,brain)"
            " VALUES(?,?,'text','router',?,?,?,?,'complete',?,?,'[]')",
            (bid, station, created, created, utc_full(created), utc_full(created),
             json.dumps([text], ensure_ascii=False), json.dumps([answer], ensure_ascii=False)))
    conn.execute("INSERT OR IGNORE INTO brain_seen(id) VALUES(?)", (bid,))


def sync_brain(conn, router_db=ROUTER_DB):
    """Copy new claude-router turns (user text + answer) into dialogs. Returns rows handled."""
    if not os.path.exists(router_db):
        return 0
    ro = sqlite3.connect(f"file:{router_db}?mode=ro", uri=True, timeout=5)
    try:
        rows = ro.execute("SELECT seq, station, prompt, answer, created FROM context ORDER BY seq").fetchall()
    finally:
        ro.close()
    n = 0
    for seq, station, prompt, answer, created in rows:
        bid = f"brain-{seq}-{int(created)}"
        if conn.execute("SELECT 1 FROM brain_seen WHERE id=?", (bid,)).fetchone():
            continue
        _attach_or_insert_brain(conn, bid, station, created, prompt, answer)
        n += 1
    conn.commit()
    return n


def import_adapter_turns(conn, adapter_db=ADAPTER_DB):
    """One-off backfill: adapter turns carry no text, but prove a dialog happened. Skips turns
    already covered by a dialog within 90 s on the same station."""
    if not os.path.exists(adapter_db):
        return 0
    ro = sqlite3.connect(f"file:{adapter_db}?mode=ro", uri=True, timeout=5)
    try:
        rows = ro.execute("SELECT id, station, status, started FROM turns ORDER BY started").fetchall()
    finally:
        ro.close()
    n = 0
    for tid, station, status, started in rows:
        if conn.execute("SELECT 1 FROM dialogs WHERE station=? AND ts_start BETWEEN ? AND ?",
                        (station, started - 90, started + 90)).fetchone():
            continue
        err = "ход прерван" if status in ("interrupted", "running") else ""
        cur = conn.execute(
            "INSERT OR IGNORE INTO dialogs(id,station,kind,src,ts_start,ts_end,start_full,end_full,status,errors,raw)"
            " VALUES(?,?,'text','adapter',?,?,?,?,?,?,?)",
            (f"adapter-{tid}", station, started, started, utc_full(started), utc_full(started),
             "complete" if status == "complete" else status, err,
             json.dumps(["ход ИИ из adapter.sqlite3; текст реплик тогда не сохранялся"], ensure_ascii=False)))
        n += cur.rowcount
    conn.commit()
    return n


def sync_journal(conn, unit=ADAPTER_UNIT):
    """Append adapter journal lines to raw_log (cursor in meta; dedupe makes overlap harmless)."""
    r = conn.execute("SELECT v FROM meta WHERE k='journal_since'").fetchone()
    cmd = ["journalctl", "--user", "-u", unit, "--no-pager", "-o", "short-iso"]
    cmd += ["--since", "@%d" % int(float(r["v"]))] if r else []
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, errors="replace")
    except Exception:
        return 0
    n, last = 0, None
    for raw in p.stdout.splitlines():
        m = JOURNAL.match(raw)
        if not m:
            continue
        t = datetime.fromisoformat(m.group(1)).timestamp()
        dts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + (".%06d000Z" % int((t % 1) * 1e6))
        n += store_line(conn, None, dts, "journal:" + unit, m.group(2)[:600])
        last = t
    if last is not None:
        conn.execute("INSERT INTO meta(k,v) VALUES('journal_since',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(last - 5),))
    conn.commit()
    return n


def prune(conn, days=RETENTION_DAYS):
    cut = time.time() - days * 86400
    conn.execute("DELETE FROM raw_log WHERE ts<?", (cut,))
    conn.execute("DELETE FROM dialog_turns WHERE ts<?", (cut,))
    conn.execute("DELETE FROM dialogs WHERE ts_start<?", (cut,))
    conn.execute("DELETE FROM voice_scores WHERE ts<?", (cut,))
    conn.commit()


# ---------- docker follower ----------
class Ingester(threading.Thread):
    def __init__(self, path=DB_PATH, docker=("docker",), interval=3.0, router_db=ROUTER_DB,
                 adapter_db=ADAPTER_DB, journal=True, on_log=None):
        super().__init__(daemon=True, name="dialog-ingester")
        self.path, self.docker, self.interval = path, list(docker), interval
        self.router_db, self.adapter_db, self.journal, self.on_log = router_db, adapter_db, journal, on_log
        self.stop_ev = threading.Event()
        self.readers = {}      # cid -> (thread, launched_state)
        self.procs = {}
        self.status = {"containers": [], "running": False, "dockerOk": None, "cycle": 0}
        self._last = {"brain": 0, "journal": 0, "prune": 0}
        self.conn = init_db(path)
        try:
            backfill_turns(self.conn)
        except sqlite3.Error as e:
            print(f"dialog-ingester: turns backfill: {e}", file=sys.stderr, flush=True)
        self.conn.close()

    def _run(self, args, timeout=20):
        p = subprocess.run(self.docker + args, capture_output=True, text=True, timeout=timeout, errors="replace")
        return p.returncode, p.stdout

    def containers(self):
        rc, out = self._run(["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"])
        if rc != 0:
            return None
        ids = [i for i, n in (l.split("\t", 1) for l in out.splitlines() if "\t" in l)
               if CONTAINER_RE.match(n.split(",")[0])]
        if not ids:
            return []
        rc, out = self._run(["inspect", "-f", "{{.Id}}\t{{.Name}}\t{{.State.Running}}\t{{.State.StartedAt}}\t{{.State.FinishedAt}}"] + ids)
        res = []
        for l in out.splitlines():
            f = l.split("\t")
            if len(f) == 5:
                res.append({"id": f[0], "name": f[1].lstrip("/"), "running": f[2] == "true", "state": (f[2], f[3], f[4])})
        return res

    def _follow(self, c):
        conn = connect(self.path)
        src = "docker:" + c["name"]
        try:
            parser = DialogParser(conn, src)
            r = conn.execute("SELECT dts FROM cursors WHERE cid=?", (c["id"],)).fetchone()
            cmd = self.docker + ["logs", "-t", "-f"] + (["--since", r["dts"]] if r else []) + [c["id"]]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.procs[c["id"]] = proc
            for raw in proc.stdout:
                text = raw.decode("utf-8", "replace").rstrip("\n")
                dts, _, line = text.partition(" ")
                if dts_epoch(dts) is None:
                    continue
                if store_line(conn, parser, dts, src, line) and self.on_log:
                    self._notify(line)
                conn.execute("INSERT INTO cursors(cid,dts) VALUES(?,?) ON CONFLICT(cid) DO UPDATE SET dts=excluded.dts"
                             " WHERE excluded.dts>cursors.dts", (c["id"], dts))
                conn.commit()
            proc.wait()
        except Exception as e:
            print(f"dialog-ingester: reader {c['name']}: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()

    def _notify(self, line):
        """Live (not history replay) container log message -> on_log(epoch, message); never raises."""
        m = LINE.match(ANSI.sub("", line.rstrip("\r\n")))
        if not m:
            return
        try:
            t = to_ts(m.group(1))
            if time.time() - t <= LIVE_LOG_S:
                self.on_log(t, m.group(4))
        except Exception as e:
            print(f"dialog-ingester: on_log: {e}", file=sys.stderr, flush=True)

    def _ensure_readers(self, cs):
        for c in cs:
            th, launched = self.readers.get(c["id"], (None, None))
            alive = th is not None and th.is_alive()
            if alive or (launched == c["state"] and not c["running"]):
                continue
            th = threading.Thread(target=self._follow, args=(c,), daemon=True, name="dialog-follow-" + c["name"])
            self.readers[c["id"]] = (th, c["state"])
            th.start()

    def cycle(self):
        now = time.time()
        cs = self.containers()
        self.status["dockerOk"] = cs is not None
        if cs is not None:
            self.status["containers"] = [{"name": c["name"], "running": c["running"]} for c in cs]
            self.status["running"] = any(c["running"] for c in cs)
            self._ensure_readers(cs)
        conn = connect(self.path)
        try:
            if now - self._last["brain"] >= 3:
                self._last["brain"] = now
                sync_brain(conn, self.router_db)
                if not conn.execute("SELECT 1 FROM meta WHERE k='adapter_turns_imported'").fetchone():
                    import_adapter_turns(conn, self.adapter_db)   # after sync_brain: text rows win over textless turns
                    conn.execute("INSERT INTO meta(k,v) VALUES('adapter_turns_imported',?)", (str(now),))
                    conn.commit()
            if self.journal and now - self._last["journal"] >= 60:
                self._last["journal"] = now
                sync_journal(conn)
            if now - self._last["prune"] >= 3600:
                self._last["prune"] = now
                prune(conn)
        finally:
            conn.close()
        self.status["cycle"] += 1

    def join_readers(self, timeout=10):
        for th, _ in list(self.readers.values()):
            th.join(timeout)

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self.cycle()
            except Exception as e:
                print(f"dialog-ingester: {e}", file=sys.stderr, flush=True)
            self.stop_ev.wait(self.interval)

    def stop(self):
        self.stop_ev.set()
        for p in list(self.procs.values()):
            if p.poll() is None:
                p.terminate()


# ---------- read side ----------
def row_to_api(r, now):
    t0, t1, status, err, kind = r["ts_start"], r["ts_end"], r["status"], r["errors"], r["kind"]
    running = status in ("", "running") and now - t0 < STALE_RUNNING_S
    if status in ("", "running") and not running:
        status = "оборван"
    ok = status in ("serverFinished", "complete") and not err
    return {"id": r["id"], "station": r["station"], "kind": kind, "ts": t0,
            "start": loc_hms(t0), "date": time.strftime("%Y-%m-%d", time.localtime(t0)),
            "startFull": r["start_full"], "endFull": r["end_full"] or "",
            "end": loc_hms(t1) if t1 else "", "dur": round(t1 - t0, 1) if t1 else "",
            "status": "идёт…" if running else status, "ok": ok, "running": running, "err": err,
            "heard": json.loads(r["heard"]), "said": json.loads(r["answered"]),
            "reqs": json.loads(r["actions"]), "brain": json.loads(r["brain"]),
            "raw": json.loads(r["raw"])[-80:]}


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _bound(v, upper):
    """(epoch, strict) for a filter bound. YYYY-MM-DD is a local date: start of that day, and for an upper
    bound the start of the next day (exclusive). Numbers are epoch seconds, inclusive. None/'' = unbounded."""
    if v is None or v == "":
        return None, False
    if isinstance(v, str) and _DATE_RE.fullmatch(v.strip()):
        day = datetime.strptime(v.strip(), "%Y-%m-%d")
        return (day + timedelta(days=1) if upper else day).timestamp(), upper
    return float(v), False


def _haystack(d):
    parts = [*d["heard"], *d["said"]]
    for a in d["reqs"]:
        parts += [str(a.get("text", "")), str(a.get("res", ""))]
    for b in d["brain"]:
        parts += [str(b.get("prompt", "")), str(b.get("answer", ""))]
    return "\n".join(parts).casefold()


def _filtered(conn, limit, before, q, since, until, station):
    """Newest-first dialogs matching all filters, up to limit+1 (the extra one tells hasMore).
    Text match is done in Python: SQLite LIKE/lower() are ASCII-only and would miss Cyrillic case."""
    needle = (q or "").strip().casefold()
    lo, _ = _bound(since, False)
    hi, hi_strict = _bound(until, True)
    where = ["(? IS NULL OR ts_start<?)", "(? IS NULL OR ts_start>=?)",
             "(? IS NULL OR ts_start" + ("<" if hi_strict else "<=") + "?)", "(? IS NULL OR station=?)"]
    args = [before, before, lo, lo, hi, hi, station, station]
    cur = conn.execute("SELECT * FROM dialogs WHERE " + " AND ".join(where) + " ORDER BY ts_start DESC, id", args)
    now = time.time()
    out = []
    for r in cur:
        d = row_to_api(r, now)
        if needle and needle not in _haystack(d):
            continue
        out.append(d)
        if len(out) > limit:
            break
    _attach_turns(conn, out)
    return out


def _fallback_turns(d):
    """Turns for dialogs stored before per-line timestamps (or without log lines: router/adapter rows). Only the
    pairing is known, not the real moments: heard[i] -> said[i], spread 1 ms apart from the dialog start."""
    base, turns, user_kind = d["ts"], [], "text" if d["kind"] == "text" else "speech"
    for i in range(max(len(d["heard"]), len(d["said"]))):
        if i < len(d["heard"]):
            turns.append({"ts": base + i * 2e-3, "role": "user", "kind": user_kind, "text": d["heard"][i], "res": ""})
        if i < len(d["said"]):
            turns.append({"ts": base + i * 2e-3 + 1e-3, "role": "assistant", "kind": "speech", "text": d["said"][i], "res": ""})
    if not turns:
        for i, b in enumerate(d["brain"]):
            turns.append({"ts": b.get("ts", base) + i * 2e-3, "role": "user", "kind": "text", "text": b.get("prompt", ""), "res": ""})
            turns.append({"ts": b.get("ts", base) + i * 2e-3 + 1e-3, "role": "assistant", "kind": "speech",
                          "text": b.get("answer", ""), "res": ""})
    for i, a in enumerate(d["reqs"]):
        turns.append({"ts": base + 1e-4 * i, "role": "event", "kind": "action" if a.get("kind") == "действие" else "intent",
                      "text": a.get("text", ""), "res": a.get("res", "")})
    return turns


def _attach_turns(conn, dialogs):
    """d["turns"]: the dialog as a chat, ordered by real log time. An intent event whose result is exactly the
    spoken reply is dropped (the reply bubble already says it)."""
    ids = [d["id"] for d in dialogs]
    by = {i: [] for i in ids}
    try:
        for k in range(0, len(ids), 400):
            chunk = ids[k:k + 400]
            rows = conn.execute("SELECT * FROM dialog_turns WHERE dialog_id IN (%s) ORDER BY ts, id" % ",".join("?" * len(chunk)),
                                chunk).fetchall()
            for r in rows:
                by[r["dialog_id"]].append({"ts": r["ts"], "role": r["role"], "kind": r["kind"], "text": r["text"], "res": r["res"]})
    except sqlite3.Error:
        pass
    for d in dialogs:
        turns = by[d["id"]]
        d["turnsApprox"] = not turns
        if not turns:
            turns = _fallback_turns(d)
        spoken = {t["text"] for t in turns if t["role"] == "assistant"}
        turns = [t for t in turns if not (t["kind"] == "intent" and t["res"] in spoken)]
        turns.sort(key=lambda t: t["ts"])
        for t in turns:
            t["t"] = loc_hms(t["ts"])
        d["turns"] = turns


CLIP_MATCH_S = 8.0


def attach_media(d):
    """Hang each archived clip and speaker score on the user turn nearest in time (within CLIP_MATCH_S, one
    each); a clip nobody heard (gate / blacklist) becomes its own user turn so dropped speech shows in order."""
    turns = d.get("turns")
    if turns is None:
        return
    users = [t for t in turns if t["role"] == "user" and t["kind"] == "speech"]

    def nearest(ts, key):
        free = [t for t in users if key not in t and abs(t["ts"] - ts) <= CLIP_MATCH_S]
        return min(free, key=lambda t: abs(t["ts"] - ts)) if free else None

    for v in d.get("voice", []):
        t = nearest(v.get("tsRaw", d["ts"]), "voice")
        if t is not None:
            t["voice"] = v
    for c in d.get("clips", []):
        t = nearest(c["ts"], "clip")
        if t is not None:
            t["clip"] = c
        else:
            turns.append({"ts": c["ts"], "t": loc_hms(c["ts"]), "role": "user", "kind": "dropped",
                          "text": c.get("transcript") or "", "res": "", "clip": c})
    turns.sort(key=lambda t: t["ts"])


def page(conn, limit=50, before=None, q=None, since=None, until=None):
    limit = max(1, min(int(limit), 200))
    rows = _filtered(conn, limit, before, q, since, until, None)
    out = rows[:limit]
    _attach_voice(conn, out)
    return {"dialogs": out, "hasMore": len(rows) > limit, "nextBefore": out[-1]["ts"] if out else None}


def search(conn, q, since=None, until=None, limit=20, station=None):
    """API-shaped dialogs (newest first) whose text contains q (case-insensitive), within the date range."""
    return _filtered(conn, max(1, min(int(limit), 200)) - 1, None, q, since, until, station)


def _attach_voice(conn, dialogs):
    """Speaker-verification scores (one per spoken turn) onto the dialog they belong to: the one that
    started last at or before the score (STT opens within ~VOICE_LEAD_S of "Dialog started") and
    has not ended more than VOICE_LEAD_S earlier."""
    for d in dialogs:
        d["voice"] = []
    if not dialogs:
        return
    lo = min(d["ts"] for d in dialogs) - VOICE_LEAD_S
    hi = max((d["ts"] + 3600 if d["end"] == "" else d["ts"] + d["dur"]) for d in dialogs) + VOICE_LEAD_S
    rows = conn.execute("SELECT * FROM voice_scores WHERE ts BETWEEN ? AND ? ORDER BY ts", (lo, hi)).fetchall()
    for r in rows:
        owners = [d for d in dialogs if d["ts"] - VOICE_LEAD_S <= r["ts"]
                  and (d["end"] == "" or r["ts"] <= d["ts"] + d["dur"] + VOICE_LEAD_S)]
        if owners:
            max(owners, key=lambda d: d["ts"])["voice"].append(
                {"ts": loc_hms(r["ts"]), "tsRaw": r["ts"], "score": r["score"], "verdict": r["verdict"], "best": r["best"],
                 "mode": r["mode"], "text": r["text"], "ms": r["ms"]})


def voice_stats(conn, days=7):
    """Counts per verdict for the owner's shadow-mode review before switching to enforce."""
    since = time.time() - days * 86400
    rows = conn.execute("SELECT verdict, count(*) n, avg(score) a, min(score) lo, max(score) hi FROM voice_scores"
                        " WHERE ts>? GROUP BY verdict", (since,)).fetchall()
    return {"days": days, "verdicts": {r["verdict"] or "?": {"n": r["n"], "avg": r["a"], "min": r["lo"], "max": r["hi"]} for r in rows}}


def log_summary(conn):
    now = time.time()
    r = conn.execute("SELECT ts FROM raw_log WHERE src LIKE 'docker:%' ORDER BY ts DESC LIMIT 1").fetchone()
    last = r["ts"] if r else None
    recent = conn.execute("SELECT count(*) FROM dialogs WHERE COALESCE(ts_end, ts_start)>?", (now - 300,)).fetchone()[0]
    return {"lastLogTs": utc_full(last) if last else "", "lastLogLocal": loc_hms(last) if last else "",
            "lastLogAge": round(now - last) if last else None, "recent5m": recent,
            "total": conn.execute("SELECT count(*) FROM dialogs").fetchone()[0]}


def stats(conn, path=DB_PATH):
    q = lambda s: conn.execute(s).fetchone()[0]
    size = sum(os.path.getsize(path + sfx) for sfx in ("", "-wal", "-shm") if os.path.exists(path + sfx))
    lo, hi = q("SELECT min(ts_start) FROM dialogs"), q("SELECT max(ts_start) FROM dialogs")
    return {"path": path, "bytes": size, "dialogs": q("SELECT count(*) FROM dialogs"),
            "rawLines": q("SELECT count(*) FROM raw_log"), "retentionDays": RETENTION_DAYS,
            "earliest": utc_full(lo) if lo else "", "earliestLocal": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(lo)) if lo else "",
            "latestLocal": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hi)) if hi else "",
            "sources": {r[0]: r[1] for r in conn.execute("SELECT src, count(*) FROM raw_log GROUP BY src")},
            "dialogSources": {r[0]: r[1] for r in conn.execute("SELECT src, count(*) FROM dialogs GROUP BY src")}}


def rebuild(conn):
    """Re-derive docker dialogs from raw_log (after a parser change); keeps attached brain turns."""
    keep = {r["id"]: r["brain"] for r in conn.execute("SELECT id, brain FROM dialogs WHERE brain!='[]' AND src LIKE 'docker:%'")}
    conn.execute("DELETE FROM dialog_turns WHERE dialog_id IN (SELECT id FROM dialogs WHERE src LIKE 'docker:%')")
    conn.execute("DELETE FROM dialogs WHERE src LIKE 'docker:%'")
    for (src,) in conn.execute("SELECT DISTINCT src FROM raw_log WHERE src LIKE 'docker:%'").fetchall():
        parser = DialogParser(conn, src)
        for (line,) in conn.execute("SELECT line FROM raw_log WHERE src=? ORDER BY dts, id", (src,)).fetchall():
            parser.feed(conn, line)
    for did, brain in keep.items():
        conn.execute("UPDATE dialogs SET brain=? WHERE id=?", (brain, did))
    conn.commit()


if __name__ == "__main__":
    c = init_db()
    if sys.argv[1:] == ["rebuild"]:
        rebuild(c)
    print(json.dumps(stats(c), ensure_ascii=False, indent=1))

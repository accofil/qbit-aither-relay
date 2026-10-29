"""Aither relay: NVMe -> local HDD -> remote seedbox, then retire the local copy.

Runs as a long-lived loop (systemd service). Torrents are selected by qBittorrent category.
Every torrent goes through these stages, persisted in a JSON state file so a restart resumes
where it stopped:

  moving       local qBittorrent moves the data from the fast disk to the HDD folder
  on_hdd       waiting for a free transfer slot (data on the HDD, or left in place for cross-seeds)
  added        the .torrent is added (stopped) to the remote qBittorrent
  transferring rsync copies the files to the remote box
  verifying    remote qBittorrent re-hashes the data; when it is complete the remote torrent
               is started and the local one retired right away
  done         local torrent removed (list-only for cross-seeds, otherwise with data)
  manual       something needs a human; the local torrent is left untouched

A transfer slot is freed as soon as rsync finishes, so the next transfer never waits for the
remote hash check - only the local deletion does.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import posixpath
import shlex
import signal
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .paths import absolute_files, hardlinked_files, is_under, sharing_torrents
from .qbit import Qbit, QbitError

log = logging.getLogger("aither-relay")

CHECKING_STATES = {"checkingUP", "checkingDL", "checkingResumeData", "moving", "metaDL", "allocating"}
STAGE_TAGS = {
    "moving": "relay:hdd-move",
    "on_hdd": "relay:queued",
    "added": "relay:sending",
    "transferring": "relay:sending",
    "verifying": "relay:remote-check",
    "manual": "relay:MANUAL",
}
ALL_TAGS = ",".join(sorted(set(STAGE_TAGS.values())))
MOVE_RETRY_MINUTES = 15
MAX_MOVE_TRIES = 3


# ----------------------------------------------------------------------------- config
@dataclass
class Config:
    src_url: str
    src_user: str
    src_pass: str
    fast_root: str
    hdd_dir: str
    dst_ssh_host: str
    dst_qbit_port: int
    dst_user: str
    dst_pass: str
    dst_root: str
    dst_dir: str
    categories: list[str]
    dst_rsync_url: str = ""
    dst_rsync_password_file: str = ""
    move_after_minutes: int = 30
    max_parallel_transfers: int = 2
    min_free_gb_remote: int = 100
    poll_seconds: int = 20
    verify_timeout_hours: float = 6
    max_transfer_attempts: int = 3
    tunnel_port: int = 18395
    state_dir: str = "~/.local/state/aither-relay"
    rsync_extra: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(os.path.expanduser(path), "rb") as fh:
            c = tomllib.load(fh)
        s, d, r = c["source"], c["target"], c.get("relay", {})
        cfg = cls(
            src_url=s["url"], src_user=s["username"], src_pass=s["password"],
            fast_root=s["fast_root"], hdd_dir=s["hdd_dir"],
            dst_ssh_host=d["ssh_host"], dst_qbit_port=int(d["qbit_port"]),
            dst_user=d["username"], dst_pass=d["password"], dst_root=d["root"], dst_dir=d["dir"],
            categories=r.get("categories", ["aither"]),
            dst_rsync_url=d.get("rsync_url", ""), dst_rsync_password_file=d.get("rsync_password_file", ""),
            **{k: r[k] for k in (
                "move_after_minutes", "max_parallel_transfers", "min_free_gb_remote", "poll_seconds",
                "verify_timeout_hours", "max_transfer_attempts", "tunnel_port", "state_dir", "rsync_extra",
            ) if k in r},
        )
        if not is_under(cfg.dst_dir, cfg.dst_root):
            raise ValueError(f"target.dir {cfg.dst_dir} must be inside target.root {cfg.dst_root}")
        return cfg

    @property
    def state_path(self) -> Path:
        return Path(os.path.expanduser(self.state_dir))


# ----------------------------------------------------------------------------- state
class State:
    def __init__(self, path: Path):
        self.path = path / "state.json"
        path.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}
        if self.path.exists():
            self.items = json.loads(self.path.read_text())
        for st in self.items.values():  # stage removed in v2: remote seeding is no longer awaited
            if st.get("stage") == "starting":
                st["stage"] = "verifying"

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=1, ensure_ascii=False))
        tmp.replace(self.path)


# ----------------------------------------------------------------------------- tunnel
class Tunnel:
    """ssh -L forward to the remote qBittorrent WebUI (it only listens on 127.0.0.1)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None

    def _port_open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(2)
            return s.connect_ex(("127.0.0.1", self.cfg.tunnel_port)) == 0

    def ensure(self) -> None:
        if self.proc and self.proc.poll() is None and self._port_open():
            return
        self.close()
        cmd = ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
               "-L", f"127.0.0.1:{self.cfg.tunnel_port}:127.0.0.1:{self.cfg.dst_qbit_port}", self.cfg.dst_ssh_host]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(30):
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
                raise QbitError(f"ssh tunnel exited: {err}")
            if self._port_open():
                log.info("ssh tunnel to %s up on 127.0.0.1:%s", self.cfg.dst_ssh_host, self.cfg.tunnel_port)
                return
            time.sleep(1)
        raise QbitError("ssh tunnel did not come up in 30 s")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


# ----------------------------------------------------------------------------- relay
class Relay:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.src = Qbit(cfg.src_url, cfg.src_user, cfg.src_pass)
        self.dst = Qbit(f"http://127.0.0.1:{cfg.tunnel_port}", cfg.dst_user, cfg.dst_pass,
                        host_header=f"127.0.0.1:{cfg.dst_qbit_port}")
        self.tunnel = Tunnel(cfg)
        self.state = State(cfg.state_path)
        self.transfers: dict[str, subprocess.Popen] = {}
        self._file_names: dict[str, list[str]] = {}
        self._stop = False

    # -- helpers -------------------------------------------------------------------
    def _set(self, h: str, stage: str, **extra) -> None:
        item = self.state.items.setdefault(h, {})
        old = item.get("stage")
        item.update(extra, stage=stage, since=time.time())
        self.state.save()
        if stage != old:
            log.info("[%s] %s -> %s  %s", h[:8], old or "new", stage, item.get("name", ""))
            self._tag(h, stage)

    def _tag(self, h: str, stage: str) -> None:
        try:
            self.src.remove_tags(h, ALL_TAGS)
            if stage in STAGE_TAGS:
                self.src.add_tags(h, STAGE_TAGS[stage])
        except QbitError as exc:
            log.debug("tagging %s failed: %s", h[:8], exc)

    def _manual(self, h: str, reason: str) -> None:
        log.error("[%s] MANUAL: %s", h[:8], reason)
        self._set(h, "manual", error=reason)

    def _names(self, client: Qbit, h: str, cache: bool = True) -> list[str]:
        if cache and client is self.src and h in self._file_names:
            return self._file_names[h]
        names = [f["name"] for f in sorted(client.files(h), key=lambda f: f["index"])]
        if client is self.src:
            self._file_names[h] = names
        return names

    def _file_sets(self, torrents: list[dict]) -> dict[str, set[str]]:
        return {t["hash"]: absolute_files(t["save_path"], [{"name": n} for n in self._names(self.src, t["hash"])])
                for t in torrents}

    def _shared(self, h: str, torrents: dict[str, dict]) -> str:
        """Why the torrent's data is also used by another torrent ('' if it is not).

        Two kinds of cross-seed: another torrent pointing at the same file paths, or the files
        being hardlinked elsewhere (cross-seed tools link into their own folder)."""
        file_sets = self._file_sets(list(torrents.values()))
        sharing = sharing_torrents(h, file_sets)
        if sharing:
            names = ", ".join(sorted(torrents[x]["name"][:40] for x in sharing)[:3])
            return f"{len(sharing)} other torrent(s) use the same files ({names})"
        linked = hardlinked_files(file_sets[h])
        if linked:
            return f"{len(linked)} file(s) are hardlinked elsewhere"
        return ""

    def _remote_free_bytes(self) -> int:
        cmd = ["ssh", "-o", "BatchMode=yes", self.cfg.dst_ssh_host,
               f"mkdir -p {shlex.quote(self.cfg.dst_dir)} && df -B1 --output=avail {shlex.quote(self.cfg.dst_dir)} | tail -1"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout
        return int(out.strip())

    # -- main loop -----------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        log.info("aither-relay started (dry_run=%s, categories=%s), source qBittorrent %s",
                 self.dry_run, self.cfg.categories, self.src.version())
        try:
            while not self._stop:
                try:
                    self.tick()
                except (QbitError, OSError, subprocess.SubprocessError) as exc:
                    log.warning("tick failed: %s", exc)
                if once:
                    break
                for _ in range(self.cfg.poll_seconds):
                    if self._stop:
                        break
                    time.sleep(1)
        finally:
            for h, proc in self.transfers.items():
                if proc.poll() is None:
                    log.info("[%s] stopping rsync (will resume on next start)", h[:8])
                    proc.terminate()
            self.tunnel.close()

    def tick(self) -> None:
        torrents = {t["hash"]: t for t in self.src.torrents()}
        now = time.time()

        # forget torrents that vanished from the local client outside of our control,
        # and prune finished entries after 30 days
        for h in list(self.state.items):
            st = self.state.items[h]
            if st["stage"] == "done" and (h in torrents or now - st.get("since", now) > 30 * 86400):
                # re-added locally after we retired it: treat it as new
                del self.state.items[h]
                self.state.save()
            elif h not in torrents and st["stage"] != "done":
                log.warning("[%s] disappeared from local qBittorrent while %s - dropping state", h[:8], st["stage"])
                self.transfers.pop(h, None)
                del self.state.items[h]
                self.state.save()

        # new candidates: selected by category only
        for h, t in torrents.items():
            if h in self.state.items or t.get("category") not in self.cfg.categories:
                continue
            if t["progress"] < 1 or not t.get("completion_on"):
                continue
            if now - t["completion_on"] < self.cfg.move_after_minutes * 60:
                continue
            self.start_item(t, torrents)

        if self.dry_run:
            return
        if any(st["stage"] in ("added", "transferring", "verifying", "on_hdd")
               for st in self.state.items.values()):
            self.tunnel.ensure()

        for h, st in list(self.state.items.items()):
            t = torrents.get(h)
            if not t:
                continue
            stage = st["stage"]
            if stage == "moving":
                self.check_moved(h, t)
            elif stage == "on_hdd":
                self.maybe_start_transfer(h, t)
            elif stage == "added":
                self.launch_rsync(h, t)
            elif stage == "transferring":
                self.check_transfer(h)
            elif stage == "verifying":
                self.check_verified(h, torrents)

    # -- stage: move to HDD ----------------------------------------------------------
    def start_item(self, t: dict, torrents: dict[str, dict]) -> None:
        h = t["hash"]
        shared = self._shared(h, torrents)
        if self.dry_run:
            if shared:
                plan = f"send from {t['save_path']} without moving (cross-seed: {shared})"
            elif is_under(t["save_path"], self.cfg.fast_root):
                plan = f"move to {self.cfg.hdd_dir}, then send"
            else:
                plan = f"send from {t['save_path']}"
            log.info("[dry-run] [%s] %s (%.1f GB): %s", h[:8], t["name"], t["size"] / 1e9, plan)
            return
        self.state.items[h] = {"name": t["name"], "size": t["size"], "attempts": 0, "shared": shared}
        if shared:
            # moving would pull the data away from the other torrent, so it is sent from where it is
            log.info("[%s] cross-seed (%s): data stays in %s, sending from there", h[:8], shared, t["save_path"])
            self._set(h, "on_hdd")
        elif is_under(t["save_path"], self.cfg.fast_root):
            log.info("[%s] moving %s -> %s", h[:8], t["name"], self.cfg.hdd_dir)
            self.src.set_location(h, self.cfg.hdd_dir)
            self._set(h, "moving")
        else:
            self._set(h, "on_hdd")

    def check_moved(self, h: str, t: dict) -> None:
        st = self.state.items[h]
        if t["state"] == "moving":
            return
        if is_under(t["save_path"], self.cfg.fast_root):
            # qBittorrent gave up on the move (e.g. the HDD was full): ask again a few times
            if time.time() - st["since"] < MOVE_RETRY_MINUTES * 60:
                return
            tries = st.get("move_tries", 0) + 1
            if tries > MAX_MOVE_TRIES:
                self._manual(h, f"qBittorrent did not move the data to {self.cfg.hdd_dir} after {MAX_MOVE_TRIES} tries")
                return
            log.warning("[%s] still on the fast disk %d min after the move request - asking again (%d/%d)",
                        h[:8], MOVE_RETRY_MINUTES, tries, MAX_MOVE_TRIES)
            self.src.set_location(h, self.cfg.hdd_dir)
            self._set(h, "moving", move_tries=tries)
            return
        if t["state"] in ("missingFiles", "error") or self._missing_files(t):
            self._recheck_once(h, t, "after move")
            return
        self._file_names.pop(h, None)
        self._set(h, "on_hdd")

    def _missing_files(self, t: dict) -> list[str]:
        """Files qBittorrent lists but that are absent or have the wrong size on disk.

        qBittorrent keeps reporting a torrent as complete when a move silently failed half way,
        so the files are checked on disk before anything is sent or deleted."""
        missing = []
        for f in self.src.files(t["hash"]):
            path = os.path.join(t["save_path"], f["name"])
            try:
                if os.path.getsize(path) != f["size"]:
                    missing.append(f["name"])
            except OSError:
                missing.append(f["name"])
        return missing

    def _recheck_once(self, h: str, t: dict, why: str) -> None:
        """Start one local hash check; qBittorrent then re-downloads whatever is missing."""
        st = self.state.items[h]
        if t["state"] in CHECKING_STATES or t["progress"] < 1:
            return  # a check or the re-download is still running
        if time.time() - st.get("local_recheck_at", 0) < 30 * 60:
            return
        log.warning("[%s] local files incomplete %s - local hash check, qBittorrent re-downloads the gaps", h[:8], why)
        st["local_recheck_at"] = time.time()
        self.state.save()
        self.src.recheck(h)

    # -- stage: add to remote + rsync --------------------------------------------------
    def maybe_start_transfer(self, h: str, t: dict) -> None:
        active = sum(1 for st in self.state.items.values() if st["stage"] in ("added", "transferring"))
        if active >= self.cfg.max_parallel_transfers:
            return
        if t["state"] in CHECKING_STATES or t["progress"] < 1:
            return
        if self._missing_files(t):
            self._recheck_once(h, t, "before sending")
            return
        pending = sum(st.get("size", 0) for st in self.state.items.values() if st["stage"] in ("added", "transferring"))
        free = self._remote_free_bytes()
        need = t["size"] + pending + self.cfg.min_free_gb_remote * 1024**3
        if free < need:
            if not self.state.items[h].get("space_warned"):
                log.warning("[%s] remote has %.0f GB free, need %.0f GB - waiting", h[:8], free / 1e9, need / 1e9)
                self.state.items[h]["space_warned"] = True
            return
        dst_save = self.cfg.dst_dir
        if not self.dst.torrent(h):
            self.dst.ensure_category(t.get("category", ""))
            fields = {"savepath": dst_save, "category": t.get("category", ""), "tags": "aither-relay",
                      "stopped": "true", "paused": "true", "autoTMM": "false", "contentLayout": "Original",
                      "skip_checking": "false"}
            self.dst.add_torrent_file(self.src.export(h), f"{h}.torrent", fields)
            for _ in range(20):
                if self.dst.torrent(h):
                    break
                time.sleep(1)
            else:
                raise QbitError(f"remote did not register {h[:8]} after add")
        else:
            dst_save = self.dst.torrent(h)["save_path"]
        src_names = self._names(self.src, h, cache=False)
        dst_names = self._names(self.dst, h)
        if src_names != dst_names:
            self._manual(h, "file layout differs between local and remote torrent (renamed files or subfolder layout)")
            return
        self._set(h, "added", dst_save=dst_save, src_save=t["save_path"])
        self.launch_rsync(h, t)

    def launch_rsync(self, h: str, t: dict) -> None:
        st = self.state.items[h]
        if h in self.transfers and self.transfers[h].poll() is None:
            return
        listfile = self.cfg.state_path / f"{h}.files"
        listfile.write_text("\n".join(self._names(self.src, h)) + "\n")
        logfile = self.cfg.state_path / f"{h}.rsync.log"
        cmd = ["rsync", "-a", "--partial", "--append-verify", "--mkpath", "--protect-args",
               f"--files-from={listfile}", "--stats", *self.cfg.rsync_extra]
        if self.cfg.dst_rsync_url:
            # rsync daemon over plain TCP: OpenSSH's channel window caps a single stream at ~200 Mbit/s
            # on a transatlantic RTT, the daemon reaches line rate
            rel = posixpath.relpath(st["dst_save"], self.cfg.dst_root)
            dest = self.cfg.dst_rsync_url.rstrip("/") + ("/" if rel == "." else f"/{rel}/")
            cmd += [f"--password-file={os.path.expanduser(self.cfg.dst_rsync_password_file)}"]
        else:
            dest = f"{self.cfg.dst_ssh_host}:{st['dst_save'].rstrip('/')}/"
            cmd += ["-e", "ssh -o BatchMode=yes -o ServerAliveInterval=30 -T"]
        cmd += [st["src_save"].rstrip("/") + "/", dest]
        st["attempts"] = st.get("attempts", 0) + 1
        st["transfer_started"] = time.time()
        with open(logfile, "a") as fh:
            fh.write(f"\n=== attempt {st['attempts']} {time.ctime()}\n")
        self.transfers[h] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=open(logfile, "a"),
                                             stderr=subprocess.STDOUT)
        log.info("[%s] rsync attempt %d started (%.1f GB)", h[:8], st["attempts"], st.get("size", 0) / 1e9)
        self._set(h, "transferring")

    def check_transfer(self, h: str) -> None:
        proc = self.transfers.get(h)
        if proc is None:  # service restarted mid-transfer: rsync resumes
            t = self.src.torrent(h)
            if t:
                self.launch_rsync(h, t)
            return
        rc = proc.poll()
        if rc is None:
            return
        del self.transfers[h]
        st = self.state.items[h]
        if rc != 0:
            if st["attempts"] >= self.cfg.max_transfer_attempts:
                self._manual(h, f"rsync failed {st['attempts']} times (last exit {rc}), see {h}.rsync.log")
            else:
                log.warning("[%s] rsync exit %d, retrying", h[:8], rc)
                self._set(h, "added")
            return
        secs = max(1, time.time() - st.get("transfer_started", time.time()))
        log.info("[%s] rsync done (%s, wall clock incl. polling %.0f s)", h[:8], self._rsync_rate(h), secs)
        self.dst.recheck(h)
        self._set(h, "verifying")  # frees the transfer slot: the next transfer starts on the next pass

    def _rsync_rate(self, h: str) -> str:
        """Throughput as rsync measured it (last 'bytes/sec' summary line of this torrent's log)."""
        try:
            lines = (self.cfg.state_path / f"{h}.rsync.log").read_text(errors="replace").splitlines()
            last = next(line for line in reversed(lines) if "bytes/sec" in line)
            return f"{float(last.split()[-2].replace(',', '')) / 1e6:.0f} MB/s"
        except (OSError, StopIteration, ValueError, IndexError):
            return "rate unknown"

    # -- stage: remote verification ------------------------------------------------------
    def check_verified(self, h: str, torrents: dict[str, dict]) -> None:
        rt = self.dst.torrent(h)
        st = self.state.items[h]
        if rt is None:
            self._manual(h, "torrent vanished from remote qBittorrent during verification")
            return
        if rt["state"] in CHECKING_STATES or time.time() - st["since"] < 20:
            if time.time() - st["since"] > self.cfg.verify_timeout_hours * 3600:
                self._manual(h, f"remote hash check still running after {self.cfg.verify_timeout_hours} h")
            return
        if rt["progress"] < 1:
            if st["attempts"] >= self.cfg.max_transfer_attempts:
                self._manual(h, f"remote hash check only {rt['progress']:.2%} after {st['attempts']} transfers")
            else:
                log.warning("[%s] remote hash check %.2f%% - transferring again", h[:8], rt["progress"] * 100)
                self._set(h, "added")
            return
        if rt["state"] in ("stoppedUP", "pausedUP", "stoppedDL", "pausedDL"):
            self.dst.start(h)
        log.info("[%s] remote hash check complete, remote torrent started", h[:8])
        self.retire_local(h, torrents)

    # -- stage: retire local copy --------------------------------------------------------
    def retire_local(self, h: str, torrents: dict[str, dict]) -> None:
        t = torrents[h]
        st = self.state.items[h]
        # decided on the current state: the data was never moved away from a cross-seed, so any
        # torrent sharing it is found here, including one added after the torrent was picked up
        shared = self._shared(h, torrents)
        if shared:
            log.info("[%s] cross-seed (%s): removing from list, keeping data", h[:8], shared)
            self.src.delete(h, delete_files=False)
            outcome = "removed (data kept for cross-seed)"
        else:
            log.info("[%s] no cross-seed: removing torrent and data (%s)", h[:8], t["content_path"])
            self.src.delete(h, delete_files=True)
            outcome = "removed with data"
        for _ in range(30):
            if not self.src.torrent(h):
                break
            time.sleep(1)
        else:
            log.error("[%s] local delete did not take effect", h[:8])
            return
        self.transfers.pop(h, None)
        (self.cfg.state_path / f"{h}.files").unlink(missing_ok=True)
        st.update(stage="done", since=time.time(), outcome=outcome)
        self.state.save()
        log.info("[%s] DONE %s - %s", h[:8], st.get("name", ""), outcome)

    # -- status ------------------------------------------------------------------------
    def status(self) -> str:
        rows = []
        for h, st in sorted(self.state.items.items(), key=lambda kv: kv[1].get("since", 0)):
            age = (time.time() - st.get("since", time.time())) / 3600
            extra = st.get("error") or st.get("outcome") or ""
            rows.append(f"{st['stage']:<13} {age:6.1f}h  {st.get('size', 0) / 1e9:7.1f} GB  {h[:8]}  "
                        f"{st.get('name', '')[:70]}  {extra}")
        return "\n".join(rows) or "(nothing tracked yet)"


# ----------------------------------------------------------------------------- cli
def setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(cfg.state_path / "relay.log", maxBytes=5_000_000, backupCount=5)
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers[:] = [fh, sh]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aither-relay", description=__doc__.splitlines()[0])
    ap.add_argument("-c", "--config", default="~/.config/aither-relay/config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the relay loop (systemd service)")
    sub.add_parser("once", help="run a single pass")
    sub.add_parser("dry-run", help="only list what would be relayed, change nothing")
    sub.add_parser("status", help="show tracked torrents and their stage")
    retry = sub.add_parser("retry", help="put a MANUAL torrent back into the pipeline")
    retry.add_argument("hash_prefix")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.cmd == "status":
        print(Relay(cfg).status())
        return 0
    if args.cmd == "retry":
        relay = Relay(cfg)
        hits = [h for h in relay.state.items if h.startswith(args.hash_prefix.lower())]
        if len(hits) != 1:
            print(f"{len(hits)} matches for {args.hash_prefix}")
            return 1
        del relay.state.items[hits[0]]
        relay.state.save()
        print(f"{hits[0][:8]} reset - it will be picked up again on the next pass")
        return 0
    setup_logging(cfg, args.verbose)
    Relay(cfg, dry_run=args.cmd == "dry-run").run(once=args.cmd in ("once", "dry-run"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

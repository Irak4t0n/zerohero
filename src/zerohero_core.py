#!/usr/bin/env python3
"""0Hero core: update every installed CardputerZero store app.

Reads the Store's installed list live (newly installed apps are included
automatically), compares it with the official registry, and runs each
upgrade through the Store's own transaction flow:

  1. M5CardputerZero-AppStore --prepare-package upgrade <uuid>
     (downloads and verifies the .deb, records a pending transaction)
  2. dpkg -i on the verified .deb (apt-get -f install only if a new
     dependency is needed; dpkg avoids loading the apt index, which matters
     on a 352 MB device)
  3. M5CardputerZero-AppStore --finalize-package upgrade <uuid> <tx>
     (checks the real dpkg state and updates the Store's records)

CLI:
  zerohero            list available updates
  zerohero --apply    install all available updates
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

VERSION = "1.0.0"
REGISTRY = "https://cardputerzero.github.io/generated/registry.json"
STORE_BIN = "/usr/share/APPLaunch/bin/M5CardputerZero-AppStore"
STATE = os.environ.get("M5APPSTORE_STATE_DIR",
                       os.path.expanduser("~/.local/share/cardputerzero-appstore"))
PENDING = os.path.join(STATE, "pending-package.json")
SELF_PACKAGE = "zerohero"

ENV = ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive"]


class Update:
    def __init__(self, uuid, title, package, have, new):
        self.uuid, self.title, self.package, self.have, self.new = uuid, title, package, have, new
        self.status = "waiting"   # waiting | working | done | failed
        self.error = ""


def vtuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-+~]", str(v)))


def installed_apps():
    with open(os.path.join(STATE, "installed.json")) as f:
        inst = json.load(f)
    out = {}
    for uuid, meta in inst.items():
        base = re.sub(r"^[0-9a-f]{12,}-", "", os.path.basename(meta.get("deb_path", "")))
        m = re.match(r"(.+)_([^_]+)_[^_]+\.deb$", base)
        if m:
            out[uuid] = (m.group(1), m.group(2))
    return out


def _fetch_registry():
    """Download the registry. curl copes better than urllib with slow Wi-Fi
    on this device, so use it when present and fall back to urllib."""
    last = None
    for attempt in range(3):
        try:
            if os.path.exists("/usr/bin/curl"):
                r = subprocess.run(["curl", "-fsSL", "--retry", "2", "--connect-timeout", "20",
                                    "-m", "180", "-A", "zerohero/" + VERSION, REGISTRY],
                                   capture_output=True, timeout=200)
                if r.returncode == 0:
                    return json.loads(r.stdout)
                last = RuntimeError("curl exit %d" % r.returncode)
            else:
                req = urllib.request.Request(REGISTRY, headers={"User-Agent": "zerohero/" + VERSION})
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return json.load(resp)
        except Exception as e:  # network hiccup: try again
            last = e
    raise last or RuntimeError("registry download failed")


def registry_apps():
    reg = _fetch_registry()
    return [a for a in reg.get("apps", []) if isinstance(a, dict)]


def store_busy():
    """True if the Store has an unfinished transaction it still needs to reconcile."""
    return os.path.exists(PENDING)


def can_sudo():
    return subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0


def find_updates():
    inst = installed_apps()
    apps = registry_apps()
    by_uuid = {a["uuid"]: a for a in apps if a.get("uuid")}
    by_pkg = {(a.get("download") or {}).get("package"): a for a in apps}
    ups = []
    for uuid, (pkg, have) in inst.items():
        a = by_uuid.get(uuid) or by_pkg.get(pkg)
        if a and a.get("version") and vtuple(a["version"]) > vtuple(have):
            ups.append(Update(a["uuid"], a.get("title") or pkg, pkg, have, a["version"]))
    # update 0Hero itself last so a self-update never cuts the run short
    ups.sort(key=lambda u: (u.package == SELF_PACKAGE, u.title.lower()))
    return ups


def _last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:120] if lines else ""


def repair_pending():
    """Let the Store reconcile a leftover transaction against real dpkg state."""
    try:
        with open(PENDING) as f:
            app_id = json.load(f).get("app_id", "")
    except (OSError, ValueError):
        app_id = ""
    subprocess.run([STORE_BIN, "--repair-package-transaction", app_id],
                   capture_output=True, text=True, timeout=600)
    return not store_busy()


def helper_argv(job):
    """Build the Store's root helper command from a PACKAGE_JOB line.

    PACKAGE_JOB <action> <deb> <force_overwrite> <desktop> <transaction>
                <pending_path> <reinstall> <exec>...
    """
    action, deb, force, desktop, tx, pending, reinstall = (job + [""] * 7)[:7]
    argv = [STORE_BIN, "--package-helper", action, "--package-value", deb]
    if reinstall not in ("", "0"):
        argv.append("--package-reinstall")
    if force not in ("", "0"):
        argv.append("--package-force-overwrite")
    if desktop:
        argv += ["--package-desktop", desktop]
    argv += ["--package-transaction", tx, "--package-pending-path", pending]
    for e in job[7:]:
        if e:
            argv += ["--package-exec", e]
    return ENV + argv


def upgrade_one(u, note=lambda msg: None):
    """Upgrade one app through the Store's own transaction. None on success, else an error."""
    r = subprocess.run([STORE_BIN, "--prepare-package", "upgrade", u.uuid],
                       capture_output=True, text=True, timeout=900)
    job = None
    for line in r.stdout.splitlines():
        if line.startswith("PACKAGE_JOB\t"):
            job = line.split("\t")[1:]
    if r.returncode != 0 or not job:
        return "prepare: " + (_last_line(r.stdout + r.stderr) or "failed")
    tx = job[4] if len(job) > 4 else ""
    # The helper runs as root: verifies the .deb, resolves dependencies,
    # installs it and records completion in the pending transaction.
    proc = subprocess.Popen(helper_argv(job), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    tail = ""
    for line in proc.stdout:
        if line.startswith("PROGRESS\t"):
            parts = line.rstrip("\n").split("\t")
            if len(parts) > 5 and parts[5]:
                note(parts[5])
        elif line.strip():
            tail = line.strip()
    rc = proc.wait(timeout=1800)
    fin = subprocess.run([STORE_BIN, "--finalize-package", "upgrade", u.uuid, tx],
                         capture_output=True, text=True, timeout=300)
    if fin.returncode == 0:
        return None
    if store_busy():
        repair_pending()
    if rc != 0:
        return "install: " + (tail[:100] or "failed")
    return "finalize: " + (_last_line(fin.stdout + fin.stderr) or "failed")


def run_updates(ups, on_change=lambda u: None):
    """Upgrade each item in order, reporting status changes. Returns failures."""
    failed = []
    for u in ups:
        u.status = "working"
        on_change(u)
        try:
            u.error = upgrade_one(u, note=lambda m, u=u: (setattr(u, "error", m), on_change(u))) or ""
        except subprocess.TimeoutExpired:
            u.error = "timed out"
        except Exception as e:  # keep going; one bad package must not stop the rest
            u.error = str(e)[:120]
        u.status = "failed" if u.error else "done"
        on_change(u)
        if u.error:
            failed.append(u)
    return failed


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        print("0Hero " + VERSION)
        return 0
    if store_busy() and not repair_pending():
        print("The Store has an unfinished transaction it could not reconcile.")
        print("Open the Store once, then run 0Hero again.")
        return 3
    try:
        ups = find_updates()
    except Exception as e:
        print("Could not check for updates: %s" % e)
        return 2
    if not ups:
        print("Everything is up to date.")
        return 0
    print("%d update(s) available:" % len(ups))
    for u in ups:
        print("  %-28s %s -> %s" % (u.title, u.have, u.new))
    if "--apply" not in argv:
        print("\nRun 'zerohero --apply' to install them.")
        return 0
    if not can_sudo():
        print("\n0Hero needs passwordless sudo (the default CardputerZero user has it).")
        return 4

    def show(u):
        if u.status == "working" and not u.error:
            print("\n%s %s -> %s" % (u.title, u.have, u.new), flush=True)
        elif u.status == "working":
            print("  " + u.error, flush=True)
        elif u.status == "done":
            print("  updated", flush=True)
        elif u.status == "failed":
            print("  FAILED: " + u.error, flush=True)

    failed = run_updates(ups, show)
    print("\nDone: %d updated, %d failed%s" % (
        len(ups) - len(failed), len(failed),
        (": " + ", ".join(u.title for u in failed)) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

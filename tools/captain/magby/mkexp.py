#!/usr/bin/env python3

import argparse, os, sys, shlex, subprocess, json, shutil
from datetime import datetime
from pathlib import Path

# Only variables we intentionally propagate into the workspace
KEYS_ALLOWED = ("FUZZER", "TARGET", "PROGRAM", "SHARED", "POLL", "TIMEOUT")

ROOT = os.path.dirname(os.path.realpath(__file__))
EXPERIMENTS_DIR = os.path.realpath(os.path.join(ROOT, "..", "experiments"))

def source_env(script_path: str) -> dict:
    if not script_path:
        return {}
    script = os.fspath(script_path)
    if not os.path.exists(script):
        print(f"[WARN] env script not found: {script}", file=sys.stderr) 
        return {}

    # Use bash -lc so login semantics & 'source' work; env -0 ensures null-separated pairs
    cmd = f"bash -lc 'set -a; source {shlex.quote(script)}; env -0'"
    out = subprocess.check_output(cmd, shell=True)
    env = {}
    for item in out.split(b"\0"):
        if not item:
            continue
        k, _, v = item.partition(b"=")
        # decode ignoring undecodable bytes rather than failing
        env[k.decode(errors='ignore')] = v.decode(errors='ignore')
    return env

def parse_overrides(pairs):
    result = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"[ERROR] --set expects KEY=VALUE, got: {p}")
        k, v = p.split("=", 1)
        result[k] = v
    return result

def get_unique_directory(base, parent):
    ts = datetime.now().strftime("%Y%m%d")
    first = os.path.join(parent, f"{base}-{ts}")
    if not os.path.exists(first):
        return first
    for i in range(1, 1000):
        cand = f"{first}-{i:03d}"
        if not os.path.exists(cand):
            return cand
    raise RuntimeError("Could not find a unique directory name after 999 tries.")

def write_file(path, text, mode=0o644):
    Path(path).write_text(text, encoding="utf-8")
    os.chmod(path, mode)

def get_env_allowed(env):
    return {k: env.get(k, "") for k in KEYS_ALLOWED}

def write_env_files(d, env_to_set):
    # 1) .env (KEY=VALUE) — nice for tools & quick inspection
    lines_env = [f'{k}={env_to_set.get(k, "")}' for k in KEYS_ALLOWED]
    write_file(os.path.join(d, ".env"), "\n".join(lines_env) + "\n", 0o644)

    # 2) env.sh — idempotent, source-safe (bash), echoes a short banner
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'echo "[ENV] Exporting experiment environment..."',
    ]
    for k in KEYS_ALLOWED:
        val = env_to_set.get(k, "")
        # export with proper quoting
        lines.append(f'export {k}={shlex.quote(val)}')
    lines.append('echo "[ENV] Done."')
    write_file(os.path.join(d, "env.sh"), "\n".join(lines) + "\n", 0o755)

def write_enter_sh(d):
    # Start an interactive shell with the env loaded and cwd set to workspace
    text = """#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./env.sh
# Use a clean interactive shell after env is loaded
exec bash -i
"""
    write_file(os.path.join(d, "enter.sh"), text, 0o755)

def write_check_sh(d):
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        'echo "[CHECK] Environment variables:"',
    ]
    for k in KEYS_ALLOWED:
        lines.append(f'printf "%-10s = %s\\n" "{k}" "${{{k}:-}}"')
    write_file(os.path.join(d, "check.sh"), "\n".join(lines) + "\n", 0o755)

def write_run_sh(d, cmd):
    text = f"""#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${{BASH_SOURCE[0]}}")"
source ./env.sh
./check.sh

mkdir -p tmux_log 
: > tmux_log/stdout.log
: > tmux_log/stderr.log

echo "[RUN] Starting: {cmd}"

# If running under tmux, do NOT use script (tmux already provides a tty)
if [ -n "${{TMUX-}}" ]; then
    bash -lc {shlex.quote(cmd)} \\
        > >(tee -a tmux_log/stdout.log) \\
        2> >(tee -a tmux_log/stderr.log >&2)
else
    # Outside tmux: script is OK to capture a tty transcript
    if command -v script >/dev/null 2>&1; then
        script -q -f tmux_log/tty.typescript \\
            bash -lc {shlex.quote(cmd)} \\
            > >(tee -a tmux_log/stdout.log) \\
            2> >(tee -a tmux_log/stderr.log >&2)
    else
        bash -lc {shlex.quote(cmd)} \\
            > >(tee -a tmux_log/stdout.log) \\
            2> >(tee -a tmux_log/stderr.log >&2)
    fi
fi

echo "[RUN] Finished."

# keep pane open if run in a terminal
if [ -t 0 ] && [ -t 1 ]; then
    exec bash -i
fi
"""
    write_file(os.path.join(d, "run.sh"), text, 0o755)

def write_meta_json(d, env_to_set, args):
    meta = {
        "name": args.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": d,
        "cmd": args.cmd,
        "env": env_to_set,
        "session": args.session or os.path.basename(d),
        "runner": "tmux" if args.tmux else ("screen" if args.screen else "manual"),
        "source_env_script": args.env_script,
        "overrides": parse_overrides(args.overrides),
    }
    write_file(os.path.join(d, "meta.json"), json.dumps(meta, indent=2) + "\n", 0o644)

def launch_tmux(session, workdir):
    # 1) Find tmux binary
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        raise SystemExit("[ERROR] tmux not found in PATH. Install tmux or use --screen.")

    # 2) Ensure workdir exists
    if not os.path.isdir(workdir):
        raise SystemExit(f"[ERROR] workdir does not exist: {workdir}")

    # 3) If session name is taken, append a numeric suffix
    base = session
    suffix = 0
    final = session
    while True:
        try:
            subprocess.check_call([tmux_bin, "has-session", "-t", final],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # session exists → try next suffix
            suffix += 1
            final = f"{base}-{suffix}"
        except subprocess.CalledProcessError:
            # has-session returned non-zero → session does not exist
            break

    # 4) Start a detached session that runs run.sh
    cmd = [
        tmux_bin, "new-session", "-d",
        "-s", final, "-c", workdir,
        'bash -lc "./run.sh"'
    ]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"[ERROR] Failed to start tmux session '{final}'. "
                         f"Command: {' '.join(cmd)}\nExit code: {e.returncode}")

    print(f"[TMUX] Started session '{final}'.")
    print(f"[TMUX] Attach: tmux attach -t {final}")


def launch_screen(session, workdir):
    # GNU screen detached session running run.sh
    cmd = [
        "screen", "-dmS", session, "bash", "-lc", f'cd {shlex.quote(workdir)} && ./run.sh'
    ]
    subprocess.check_call(cmd)
    print(f"[SCREEN] Started session '{session}'.")
    print(f"[SCREEN] Attach: screen -r {session}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="base name for the workspace")
    ap.add_argument("--root", default=EXPERIMENTS_DIR, help="experiments root directory")
    ap.add_argument("--env-script", default=os.path.join(ROOT, "set_default_env.sh"),
                    help="shell script to source for default env")
    ap.add_argument("--set", dest="overrides", action="append",
                    help="override/add env var (KEY=VALUE). Can repeat.")
    ap.add_argument("--cmd", default="echo 'No command provided'; sleep 1",
                    help="command to run for the experiment (quoted)")
    ap.add_argument("--tmux", action="store_true", help="launch in tmux (detached)")
    ap.add_argument("--screen", action="store_true", help="launch in GNU screen (detached)")
    ap.add_argument("--session", default=None, help="tmux/screen session name (default: workspace basename)")
    args = ap.parse_args()

    # Prepare experiment root
    root = os.path.realpath(getattr(args, "root", EXPERIMENTS_DIR))
    os.makedirs(root, exist_ok=True)

    # Load defaults then apply overrides
    env = source_env(args.env_script) if args.env_script else {}
    env.update(parse_overrides(args.overrides))

    # If defaults are not set, it should not proceed
    missing = [k for k in ("FUZZER","TARGET","PROGRAM") if not env_to_set.get(k)]
    if missing:
        raise SystemExit(f"[ERROR] Missing required env vars: {', '.join(missing)} "
                f"(did you source {args.env_script} or pass --set KEY=VALUE?)")


    # Create unique workspace
    workdir = get_unique_directory(args.name, root)
    os.makedirs(workdir, exist_ok=False)
    print(f"[INFO] created {workdir}")

    # SHARED always points to the workspace
    env["SHARED"] = workdir

    # Persist env in controlled way
    env_to_set = get_env_allowed(env)

    # Write helpers & metadata
    write_env_files(workdir, env_to_set)
    write_enter_sh(workdir)
    write_check_sh(workdir)
    write_run_sh(workdir, args.cmd)
    write_meta_json(workdir, env_to_set, args)

    # Show next steps (manual)
    print(f"[INFO] enter    : {os.path.join(workdir, 'enter.sh')}")
    print(f"[INFO] run      : (inside tmux/screen or directly) {os.path.join(workdir, 'run.sh')}")

    # Optional detached runners
    session = args.session or os.path.basename(workdir)
    if args.tmux and args.screen:
        raise SystemExit("[ERROR] Choose only one of --tmux or --screen.")
    if args.tmux:
        launch_tmux(session, workdir)
    elif args.screen:
        launch_screen(session, workdir)
    else:
        print("[INFO] Not launching tmux/screen (manual mode).")

if __name__ == "__main__":
    main()


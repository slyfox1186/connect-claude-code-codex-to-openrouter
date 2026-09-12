"""Installer/launcher regressions using isolated homes and fake external CLIs only."""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FAILS = []
CHECKS = 0


def check(label, ok, detail=""):
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


class Fixture:
    def __init__(self, base, name):
        self.base = base / name
        self.project = self.base / "project"
        self.home = self.base / "home"
        self.tools = self.base / "tools"
        self.home.mkdir(parents=True)
        self.tools.mkdir()
        for part in ("bin", "src", "config"):
            shutil.copytree(ROOT / part, self.project / part)
        shutil.copy2(ROOT / "install.sh", self.project / "install.sh")
        # Doctor can fetch the catalogue; it is outside this test's scope.
        (self.project / "bin/orask").write_text("#!/bin/sh\nexit 0\n")
        for command in ("bash", "cat", "dirname", "readlink", "date", "stat", "chmod", "mkdir",
                        "ln", "cp", "rm"):
            (self.tools / command).symlink_to(shutil.which(command))
        (self.tools / "python3").symlink_to(sys.executable)
        self.script("claude", """#!/bin/sh
printf '%s\\n' "$2" >> "$HOME/claude-calls"
case "$2" in
  remove) exit "${FAIL_REMOVE:-0}" ;;
  add-json) exit "${FAIL_ADD:-0}" ;;
esac
exit 1
""")
        self.script("codex", "#!/bin/sh\nexit 0\n")
        self.guard = self.base / "guard"
        self.guard.mkdir()
        (self.guard / "sitecustomize.py").write_text('''
import builtins, os, socket
def no_network(*args, **kwargs):
    raise AssertionError("installer tests must never use the network")
socket.create_connection = no_network
socket.socket.connect = no_network
original_replace = os.replace
def guarded_replace(source, destination):
    if os.environ.get("FAIL_REPLACE") and str(destination).endswith("config.toml"):
        raise PermissionError("injected replace failure")
    return original_replace(source, destination)
os.replace = guarded_replace
original_os_open = os.open
def guarded_backup_open(path, flags, *args, **kwargs):
    if os.environ.get("FAIL_BACKUP") and (".bak." in str(path) or str(path).endswith(".bak")):
        raise PermissionError("injected backup failure")
    return original_os_open(path, flags, *args, **kwargs)
os.open = guarded_backup_open
if os.environ.get("FAIL_CONFIG_READ"):
    original_open = builtins.open
    read_os_open = os.open
    def denied(path):
        return str(path) == os.environ["FAIL_CONFIG_READ"]
    def guarded_open(path, *args, **kwargs):
        if denied(path):
            raise PermissionError("injected config read failure")
        return original_open(path, *args, **kwargs)
    def guarded_os_open(path, flags, *args, **kwargs):
        if denied(path) and flags & os.O_ACCMODE == os.O_RDONLY:
            raise PermissionError("injected config read failure")
        return read_os_open(path, flags, *args, **kwargs)
    builtins.open = guarded_open
    os.open = guarded_os_open
''')
        self.env = {
            "HOME": str(self.home), "PATH": str(self.tools),
            "ORASK_PYTHON": sys.executable, "PYTHONPATH": str(self.guard),
            "ORASK_CONFIG_DIR": str(self.home / "config"),
            "ORASK_STATE_DIR": str(self.home / "state"),
            "ORASK_CACHE_DIR": str(self.home / "cache"),
            "OPENROUTER_API_KEY": "", "ORASK_BOOTSTRAP": "0",
        }
        self.codex = self.home / ".codex/config.toml"
        self.codex.parent.mkdir()

    def script(self, name, contents):
        path = self.tools / name
        path.unlink(missing_ok=True)
        path.write_text(contents)
        path.chmod(0o700)

    def run(self, extra=None):
        return subprocess.run(
            ["/bin/bash", str(self.project / "install.sh")],
            env=self.env | (extra or {}), cwd=self.project, input="",
            text=True, capture_output=True, timeout=30, check=False,
        )

    def calls(self):
        path = self.home / "claude-calls"
        return path.read_text().splitlines() if path.exists() else []


with tempfile.TemporaryDirectory(prefix="orask-installer-tests-") as temporary:
    scratch = Path(temporary)
    prefix = '# preserve this comment\nmodel = "example"\n'
    suffix = '[mcp_servers.other]\ncommand = "keep-me"\n'
    for name, managed in (
        ("quoted", '["mcp_servers" . \'openrouter\'] # managed server\ncommand = "old"\n'),
        ("array", '[mcp_servers.openrouter]\nargs = [\n ["nested"],\n]\n'),
        ("multiline", '[mcp_servers.openrouter]\nnote = """\n[false.header]\n"""\n'),
        ("subtables", '[mcp_servers.openrouter.env]\nOLD = "value"\n'),
    ):
        fixture = Fixture(scratch, name)
        original = prefix + managed + suffix
        fixture.codex.write_text(original)
        result = fixture.run()
        updated = fixture.codex.read_text()
        check(f"Codex {name} preserves unrelated config", result.returncode == 0
              and updated.startswith(prefix) and suffix in updated
              and "old" not in updated and "nested" not in updated
              and "false.header" not in updated and 'OLD = "value"' not in updated,
              result.stderr.strip())
        first = updated
        result = fixture.run()
        check(f"Codex {name} is idempotent", result.returncode == 0
              and fixture.codex.read_text() == first)

    fixture = Fixture(scratch, "read-error")
    fixture.codex.write_text(prefix)
    result = fixture.run({"FAIL_CONFIG_READ": str(fixture.codex)})
    check("Codex read error preserves original and fails", result.returncode != 0
          and fixture.codex.read_text() == prefix)

    fixture = Fixture(scratch, "unicode-\U0001f680")
    result = fixture.run()
    text = fixture.codex.read_text()
    check("Codex non-BMP project paths use valid TOML Unicode", result.returncode == 0
          and str(fixture.project) in text and "\\ud83d" not in text)

    for name, content in (("bad-json", "{"), ("bad-shape", "[]")):
        fixture = Fixture(scratch, name)
        claude_config = fixture.home / ".claude.json"
        claude_config.write_text(content)
        result = fixture.run()
        check(f"Claude {name} refuses mutation and fails", result.returncode != 0
              and not fixture.calls() and claude_config.read_text() == content)

    fixture = Fixture(scratch, "claude-read-error")
    claude_config = fixture.home / ".claude.json"
    claude_config.write_text("{}")
    result = fixture.run({"FAIL_CONFIG_READ": str(claude_config)})
    check("Claude read error refuses mutation and fails", result.returncode != 0
          and not fixture.calls() and claude_config.read_text() == "{}")

    for name in ("backup", "remove", "add"):
        fixture = Fixture(scratch, "claude-" + name)
        (fixture.home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"openrouter": {"command": "old"}}}))
        if name == "backup":
            fixture.script("cp", "#!/bin/sh\nexit 1\n")
        result = fixture.run({"FAIL_BACKUP": "1" if name == "backup" else "",
                              "FAIL_REMOVE": str(int(name == "remove")),
                              "FAIL_ADD": str(int(name == "add"))})
        check(f"Claude {name} failure aborts further mutation", result.returncode != 0
              and (name != "backup" or not fixture.calls())
              and (name != "remove" or fixture.calls() == ["remove"])
              and "== Done" not in result.stdout)

    fixture = Fixture(scratch, "relocated")
    claude_dir, codex_dir = fixture.home / "claude-custom", fixture.home / "codex-custom"
    claude_dir.mkdir()
    codex_dir.mkdir()
    custom_claude = claude_dir / ".claude.json"
    custom_claude.write_text(json.dumps({"mcpServers": {"openrouter": {
        "type": "stdio", "command": str(fixture.project / "bin/openrouter-mcp"),
        "args": [], "env": {"ORASK_PYTHON": sys.executable}, "timeout": 600000,
    }}}))
    fixture.codex.write_text("# default config must remain untouched\n")
    result = fixture.run({"CLAUDE_CONFIG_DIR": str(claude_dir), "CODEX_HOME": str(codex_dir)})
    check("client config overrides select the files clients actually use",
          result.returncode == 0 and not fixture.calls()
          and (codex_dir / "config.toml").exists()
          and fixture.codex.read_text() == "# default config must remain untouched\n")

    for name in ("backup", "replace"):
        fixture = Fixture(scratch, "codex-" + name)
        fixture.codex.write_text(prefix)
        result = fixture.run({"FAIL_" + name.upper(): "1"})
        check(f"Codex {name} failure preserves config and cleans temporary",
              result.returncode != 0 and fixture.codex.read_text() == prefix
              and not list(fixture.codex.parent.glob(".orask-*") ))

    fixture = Fixture(scratch, "backup-bytes")
    fixture.codex.write_bytes(prefix.replace("\n", "\r\n").encode())
    fixture.codex.chmod(0o644)
    result = fixture.run()
    backups = list(fixture.codex.parent.glob("config.toml.bak.*"))
    check("Codex backup is exact and private", result.returncode == 0 and len(backups) == 1
          and backups[0].read_bytes() == prefix.replace("\n", "\r\n").encode()
          and backups[0].stat().st_mode & 0o777 == 0o600)
    result = fixture.run()
    check("unchanged Codex registration creates no extra backup", result.returncode == 0
          and len(list(fixture.codex.parent.glob("config.toml.bak.*"))) == 1)

    fixture = Fixture(scratch, "claude-private-backup")
    claude_config = fixture.home / ".claude.json"
    claude_config.write_text('{"mcpServers": {"openrouter": {"command": "old"}}}')
    claude_config.chmod(0o644)
    result = fixture.run()
    backups = list(fixture.home.glob(".claude.json.bak.*"))
    check("Claude backup is exact and private", result.returncode == 0 and len(backups) == 1
          and backups[0].read_bytes() == claude_config.read_bytes()
          and backups[0].stat().st_mode & 0o777 == 0o600)

    for name in ("file", "directory"):
        fixture = Fixture(scratch, "collision-" + name)
        target = fixture.home / ".local/bin/orask"
        target.parent.mkdir(parents=True)
        if name == "file":
            target.write_text("user executable")
        else:
            target.mkdir()
        result = fixture.run()
        check(f"launcher {name} collision is preserved", result.returncode != 0
              and not target.is_symlink()
              and (target.read_text() == "user executable" if name == "file"
                   else not list(target.iterdir())))

    fixture = Fixture(scratch, "existing-link")
    target = fixture.home / ".local/bin/orask"
    target.parent.mkdir(parents=True)
    old_target = fixture.base / "old-directory"
    old_target.mkdir()
    target.symlink_to(old_target)
    result = fixture.run()
    check("existing launcher symlink is updated without changing its target",
          result.returncode == 0 and target.resolve() == fixture.project / "bin/orask"
          and not list(old_target.iterdir()))

    fixture = Fixture(scratch, "override")
    helper = fixture.project / "bin/_python-env.sh"
    for name, override in (("missing", str(scratch / "missing-python")),
                           ("old", str(fixture.tools / "old-python"))):
        fixture.script("old-python", "#!/bin/sh\nexit 1\n")
        result = subprocess.run(
            ["/bin/bash", "-c", 'source "$1"; orask_find_python "$2"', "_",
             str(helper), str(fixture.project)],
            env=fixture.env | {"ORASK_PYTHON": override}, check=False,
            text=True, capture_output=True, timeout=10,
        )
        check(f"explicit {name} interpreter fails without fallback", result.returncode != 0)

    fixture = Fixture(scratch, "bad-explicit-install")
    result = fixture.run({"ORASK_PYTHON": str(scratch / "missing-python"),
                          "ORASK_BOOTSTRAP": "1"})
    check("invalid explicit installer interpreter refuses before writes", result.returncode != 0
          and not (fixture.project / ".orask-python").exists()
          and not (fixture.home / ".local/bin").exists())

    fixture = Fixture(scratch, "relative-explicit-install")
    result = fixture.run({"ORASK_PYTHON": "../tools/python3"})
    check("relative explicit interpreter is refused before writes", result.returncode != 0
          and not (fixture.project / ".orask-python").exists()
          and not (fixture.home / ".local/bin").exists())

    for kind in ("symlink", "hardlink"):
        fixture = Fixture(scratch, "pin-" + kind)
        unrelated = fixture.base / "unrelated-file"
        unrelated.write_text("unrelated content must survive")
        pin = fixture.project / ".orask-python"
        if kind == "symlink":
            pin.symlink_to(unrelated)
        else:
            pin.hardlink_to(unrelated)
        result = fixture.run()
        check(f"interpreter pin {kind} cannot overwrite another file",
              unrelated.read_text() == "unrelated content must survive"
              and (result.returncode != 0 if kind == "symlink" else result.returncode == 0))

    fixture = Fixture(scratch, "pin-backup")
    pin = fixture.project / ".orask-python"
    pin.write_text("/previous/python\n")
    pin.chmod(0o644)
    result = fixture.run()
    backups = list(fixture.project.glob(".orask-python.*.bak"))
    check("interpreter pin replacement is private and backs up the old bytes",
          result.returncode == 0 and pin.read_text() == sys.executable + "\n"
          and pin.stat().st_mode & 0o777 == 0o600 and len(backups) == 1
          and backups[0].read_text() == "/previous/python\n"
          and backups[0].stat().st_mode & 0o777 == 0o600)
    result = fixture.run()
    check("unchanged interpreter pin does not create extra backups", result.returncode == 0
          and len(list(fixture.project.glob(".orask-python.*.bak"))) == 1)

    fixture = Fixture(scratch, "pin-backup-failure")
    pin = fixture.project / ".orask-python"
    pin.write_text("/previous/python\n")
    result = fixture.run({"FAIL_BACKUP": "1"})
    check("pin backup failure preserves pin and stops before registrations",
          result.returncode != 0 and pin.read_text() == "/previous/python\n"
          and not fixture.calls() and not (fixture.home / ".local/bin").exists())

    fixture = Fixture(scratch, "oversized-config")
    with fixture.codex.open("wb") as handle:
        handle.truncate(32 * 1024 * 1024 + 1)
    result = fixture.run()
    check("oversized config reports the installer limit without replacement",
          result.returncode != 0 and "32 MiB" in result.stderr
          and fixture.codex.stat().st_size == 32 * 1024 * 1024 + 1)

    fixture = Fixture(scratch, "launcher-checks")

    for launcher in ("orask", "openrouter-mcp"):
        shutil.copy2(ROOT / "bin" / launcher, fixture.project / "bin" / launcher)
        result = subprocess.run(
            [str(fixture.project / "bin" / launcher), "--help"],
            env=fixture.env | {"ORASK_PYTHON": str(scratch / "missing-python")},
            check=False, capture_output=True, text=True, timeout=10,
        )
        check(f"{launcher} invalid interpreter has no stdout", result.returncode != 0
              and not result.stdout and "ORASK_PYTHON" in result.stderr)
    guard_path = fixture.guard / "sitecustomize.py"
    with guard_path.open("a") as handle:
        handle.write('''
original_import = builtins.__import__
def without_mcp(name, *args, **kwargs):
    if name == "mcp" or name.startswith("mcp."):
        raise ModuleNotFoundError("mcp intentionally unavailable")
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_mcp
''')
    result = subprocess.run(
        [str(fixture.project / "bin/orask"), "--help"], env=fixture.env,
        check=False, capture_output=True, text=True, timeout=10,
    )
    check("CLI launcher works without the MCP dependency", result.returncode == 0
          and "usage:" in result.stdout and not result.stderr)

    # The new scanner must work without tomllib, as on the supported Python 3.10 floor.
    sys.path.insert(0, str(ROOT / "src"))
    os.environ["ORASK_CONFIG_DIR"] = str(scratch / "unit-config")
    os.environ["ORASK_STATE_DIR"] = str(scratch / "unit-state")
    os.environ["ORASK_CACHE_DIR"] = str(scratch / "unit-cache")
    from orask import install_config

    parser = install_config._toml
    fixtures = [
        prefix + '["mcp_servers".\'openrouter\'] # quote\ncommand = "old"\n' + suffix,
        prefix + '[mcp_servers.openrouter]\nargs = [\n ["nested"],\n]\n' + suffix,
        'notes = """\n[mcp_servers.openrouter]\n"""\n' + prefix + suffix,
        "notes = '''\n[mcp_servers.openrouter]\n'''\n" + prefix + suffix,
        prefix + '[mcp_servers.openrouter.env]\nA = "old"\n' + suffix
        + '[mcp_servers."openrouter"]\ncommand = "old"\n',
        prefix + '[[profiles.test.rows]]\nname = "keep"\n'
        + '[mcp_servers.openrouter]\nargs = ["#", "[fake]", "\\\\\\\""]\n' + suffix,
    ]
    for validation in (parser, None):
        install_config._toml = validation
        for index, original in enumerate(fixtures):
            updated = install_config.update_codex_text(original, "/tmp/server", sys.executable)
            preserved = suffix in updated
            if parser is not None:
                before = parser.loads(original)
                expected = before.setdefault("mcp_servers", {})
                expected["openrouter"] = parser.loads(install_config.codex_block(
                    "/tmp/server", sys.executable))["mcp_servers"]["openrouter"]
                preserved = parser.loads(updated) == before
            check(f"scanner preserves semantics {index} (tomllib={validation is not None})",
                  preserved and install_config.update_codex_text(
                      updated, "/tmp/server", sys.executable) == updated)
        for original in (
            'mcp_servers = { openrouter = { command = "old" } }\n',
            '[mcp_servers]\nopenrouter.command = "old"\n',
            '[[mcp_servers.openrouter]]\ncommand = "old"\n',
            'notes = """unterminated\n[mcp_servers.openrouter]\n',
        ):
            try:
                install_config.update_codex_text(original, "/tmp/server", sys.executable)
                refused = False
            except ValueError:
                refused = True
            check(f"scanner refuses ambiguous/invalid form (tomllib={validation is not None})",
                  refused)
    install_config._toml = parser

    locked_config = scratch / "locked/config.toml"
    locked_config.parent.mkdir()
    locked_config.write_text(prefix)
    with mock.patch.object(fcntl, "flock", side_effect=OSError("injected lock failure")):
        try:
            install_config.register_codex(
                locked_config, "/tmp/server", sys.executable, scratch / "unused-backup")
            refused = False
        except OSError:
            refused = True
    check("Codex lock failure refuses any config write", refused
          and locked_config.read_text() == prefix)

    for name in ("symlink", "broken-symlink", "directory", "fifo"):
        path = scratch / ("config-" + name)
        if name == "directory":
            path.mkdir()
        elif name == "fifo":
            os.mkfifo(path)
        else:
            path.symlink_to(locked_config if name == "symlink" else scratch / "absent")
        try:
            install_config.read_config(path)
            refused = False
        except ValueError:
            refused = True
        check(f"configuration {name} is refused without following or blocking", refused)

    held_lock = locked_config.with_name(locked_config.name + ".orask.lock")
    with held_lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            install_config.register_codex(
                locked_config, "/tmp/server", sys.executable, scratch / "held-backup")
            refused = False
        except BlockingIOError:
            refused = True
    check("concurrent installer lock refuses immediately", refused
          and locked_config.read_text() == prefix)

    existing_backup = scratch / "existing-backup"
    existing_backup.write_text("keep backup")
    try:
        install_config.register_codex(locked_config, "/tmp/server", sys.executable, existing_backup)
        refused = False
    except FileExistsError:
        refused = True
    check("existing backup cannot be replaced", refused
          and existing_backup.read_text() == "keep backup" and locked_config.read_text() == prefix)

    bounded_config = scratch / "bounded-config"
    bounded_config.write_bytes(b"123456789")
    for grew in (False, True):
        info = bounded_config.stat()
        reported = mock.Mock(st_mode=info.st_mode, st_ino=info.st_ino, st_dev=info.st_dev,
                             st_size=0 if grew else info.st_size)
        with mock.patch.object(install_config, "MAX_CONFIG_BYTES", 8, create=True), \
                mock.patch.object(os, "fstat", return_value=reported):
            try:
                install_config.read_config(bounded_config)
                refused = False
            except ValueError:
                refused = True
        check(f"configuration read is bounded even if fstat is stale (grew={grew})", refused)

if FAILS:
    print(f"\n{len(FAILS)} of {CHECKS} installer checks failed")
    raise SystemExit(1)
print(f"\nall {CHECKS} installer checks passed")

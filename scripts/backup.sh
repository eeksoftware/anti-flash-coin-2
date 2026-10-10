#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then PYTHON_BIN="$(command -v python3 || true)"; fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Yedekleme başarısız: python3 bulunamadı." >&2
  exit 1
fi
PROJECT_ROOT="$PROJECT_ROOT" "$PYTHON_BIN" - <<'PY'
import os, shutil, sqlite3, sys, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path(os.environ["PROJECT_ROOT"]) / ".env")
except ImportError:
    pass

root = Path(os.environ["PROJECT_ROOT"])
def path_env(name, default):
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else root / p

data_dir = path_env("DATA_DIR", "./data")
log_dir = path_env("LOG_DIR", "./logs")
backup_dir = path_env("BACKUP_DIR", "./backups")
backup_dir.mkdir(parents=True, exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
target = backup_dir / f"anti-flash-token-{stamp}.tar.gz"
temporary_archive = backup_dir / f".{target.name}.partial"
stage = Path(tempfile.mkdtemp(prefix="anti-flash-backup-"))

def snapshot_tree(source: Path, destination: Path):
    if not source.exists(): return
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        out = destination / relative
        if item.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        if not item.is_file(): continue
        if item.name.endswith(("-wal", "-shm")) and item.with_name(item.name[:-4]).exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if item.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            try:
                src = sqlite3.connect(item.as_uri() + "?mode=ro", uri=True, timeout=5)
                dst = sqlite3.connect(out)
                src.backup(dst)
                check = dst.execute("PRAGMA integrity_check").fetchone()[0]
                dst.close(); src.close()
                if check != "ok": raise RuntimeError(f"SQLite doğrulaması başarısız: {relative.name}")
            except Exception:
                try: out.unlink()
                except OSError: pass
                raise
        else:
            shutil.copy2(item, out)

try:
    snapshot_tree(data_dir, stage / "data")
    snapshot_tree(log_dir, stage / "logs")
    with tarfile.open(temporary_archive, "w:gz") as archive:
        archive.add(stage / "data", arcname="data") if (stage / "data").exists() else None
        archive.add(stage / "logs", arcname="logs") if (stage / "logs").exists() else None
    with tarfile.open(temporary_archive, "r:gz") as archive:
        archive.getmembers()
    os.replace(temporary_archive, target)
    archives = sorted(backup_dir.glob("anti-flash-token-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[14:]: old.unlink()
    print(f"Yedek oluşturuldu: {target}")
except Exception as exc:
    try: temporary_archive.unlink()
    except OSError: pass
    print(f"Yedekleme başarısız: {type(exc).__name__}", file=sys.stderr)
    raise SystemExit(1)
finally:
    shutil.rmtree(stage, ignore_errors=True)
PY

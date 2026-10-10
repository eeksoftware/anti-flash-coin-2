import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile


def test_backup_creates_consistent_archive_and_prunes_to_14(tmp_path):
    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "data"; logs = tmp_path / "logs"; backups = tmp_path / "backups"
    data.mkdir(); logs.mkdir()
    backups.mkdir()
    for index in range(15):
        old = backups / f"anti-flash-token-old-{index}.tar.gz"
        old.write_bytes(b"old")
        os.utime(old, (index + 1, index + 1))
    with sqlite3.connect(data / "operations.sqlite") as db:
        db.execute("CREATE TABLE sample(value INTEGER)")
        db.execute("INSERT INTO sample VALUES(42)")
    (logs / "app.log").write_text("sample log\n")
    env = os.environ.copy()
    env.update({"DATA_DIR": str(data), "LOG_DIR": str(logs), "BACKUP_DIR": str(backups),
                "PYTHON_BIN": str(root / ".venv/bin/python"), "PROJECT_ROOT": str(root)})
    result = subprocess.run([str(root / "scripts/backup.sh")], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    archives = sorted(backups.glob("anti-flash-token-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    assert len(archives) == 14
    with tarfile.open(archives[0], "r:gz") as archive:
        assert "data/operations.sqlite" in archive.getnames()
        assert "logs/app.log" in archive.getnames()
        extracted = tmp_path / "extract"; archive.extractall(extracted, filter="data")
    with sqlite3.connect(extracted / "data/operations.sqlite") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM sample").fetchone()[0] == 42
    assert (data / "operations.sqlite").exists() and (logs / "app.log").exists()

"""The Windows scripts (P4-5) exist and point at the right files. They are not
run here: there is no PowerShell or Task Scheduler on the CI runner."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def test_scripts_exist() -> None:
    for name in (
        "run_stride.ps1",
        "install_task.ps1",
        "backup_db.ps1",
        "run_stride.sh",
        "install_cron.sh",
        "backup_db.sh",
    ):
        assert (SCRIPTS / name).is_file(), name


def test_linux_scripts_run_both_instances_in_auto_mode() -> None:
    text = _text("run_stride.sh")
    assert '"config.paper_b.yaml" "config.paper_dip.yaml"' in text
    assert "python -m stride.run_daily --config" in text
    assert "--phase auto" in text
    assert "backup_db.sh" in text


def test_run_script_runs_both_instances_in_auto_mode() -> None:
    text = _text("run_stride.ps1")
    assert '"config.paper_b.yaml", "config.paper_dip.yaml"' in text  # B, then DIP
    assert "-m stride.run_daily --config $Config --phase auto" in text
    assert ".venv\\Scripts\\Activate.ps1" in text
    assert "data\\logs" in text and "scheduler.log" in text
    assert "exit $Failed" in text
    assert "backup_db.ps1" in text
    for config in ("config.paper_b.yaml", "config.paper_dip.yaml"):
        assert (ROOT / config).is_file()


def test_install_script_registers_one_safe_task() -> None:
    text = _text("install_task.ps1")
    assert '$TaskName = "STRIDE paper"' in text
    assert "run_stride.ps1" in text
    assert "New-TimeSpan -Minutes 15" in text
    settings = (
        "-WakeToRun",
        "-StartWhenAvailable",
        "-MultipleInstances IgnoreNew",
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 30)",
        "-LogonType S4U",
    )
    for setting in settings:
        assert setting in text, setting
    assert text.count("Register-ScheduledTask") == 1
    assert "-Password" not in text  # no password, in the repository or anywhere


def test_backup_script_keeps_30_days_in_documents() -> None:
    text = _text("backup_db.ps1")
    assert 'Documents\\stride_backups"' in text
    assert "[int]$KeepDays = 30" in text
    assert '"*.db"' in text


def test_server_setup_script_installs_cron_and_never_copies_secrets() -> None:
    text = _text("setup_server.sh")
    assert "python3.12 -m venv .venv" in text
    assert "install_cron.sh" in text
    assert "chmod 600 .env" in text
    assert "--verdict" in text


def test_no_script_mentions_live_trading() -> None:
    for path in [*SCRIPTS.glob("*.ps1"), *SCRIPTS.glob("*.sh")]:
        text = path.read_text(encoding="utf-8")
        assert "LIVE_CONFIRM" not in text and "mode: live" not in text, path.name

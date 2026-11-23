import os

from meshwatch import system


def _hwmon(tmp_path, alarm):
    real = tmp_path / "devices" / "rpi_volt"
    real.mkdir(parents=True)
    (real / "name").write_text("rpi_volt\n")
    (real / "in0_lcrit_alarm").write_text(f"{alarm}\n")
    cls = tmp_path / "class"
    cls.mkdir()
    os.symlink(real, cls / "hwmon0")  # come in /sys/class/hwmon
    return cls, real


def test_hwmon_symlink(tmp_path, monkeypatch):
    cls, real = _hwmon(tmp_path, 0)
    monkeypatch.setattr(system, "THROTTLE_PATHS", (str(tmp_path / "missing"),))
    monkeypatch.setattr(system, "HWMON_DIR", str(cls))
    monkeypatch.setattr(system, "_UV_SEEN", False)
    t = system._throttled()
    assert t["undervoltage_now"] is False and t["undervoltage_since_boot"] is False
    (real / "in0_lcrit_alarm").write_text("1\n")
    assert system._throttled()["undervoltage_now"] is True
    (real / "in0_lcrit_alarm").write_text("0\n")
    t = system._throttled()
    assert t["undervoltage_now"] is False and t["undervoltage_since_boot"] is True


def test_firmware_bits(tmp_path, monkeypatch):
    f = tmp_path / "get_throttled"
    f.write_text("50000\n")
    monkeypatch.setattr(system, "THROTTLE_PATHS", (str(f),))
    t = system._throttled()
    assert t["undervoltage_now"] is False and t["undervoltage_since_boot"] is True

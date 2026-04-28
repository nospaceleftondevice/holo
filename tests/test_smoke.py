import holo
from holo.cli import main


def test_version_attr():
    assert isinstance(holo.__version__, str)
    assert holo.__version__ != ""


def test_cli_version_flag(capsys):
    rc = main(["--version"])
    assert rc == 0
    captured = capsys.readouterr()
    assert holo.__version__ in captured.out


def test_cli_default(capsys):
    rc = main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "holo" in captured.out

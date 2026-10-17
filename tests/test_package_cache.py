"""A source package keeps an empty Docker cache input, never cached payloads."""
import importlib.util
import zipfile
from pathlib import Path


def test_source_package_excludes_archive_cache_and_preserves_build_directory(tmp_path, monkeypatch):
    script = Path(__file__).parents[1] / "scripts/package.py"
    spec = importlib.util.spec_from_file_location("source_package_cache_test", script)
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    monkeypatch.setattr(package, "ROOT", tmp_path)
    hermes = tmp_path / "hermes"
    cache = hermes / ".upstream-cache"
    cache.mkdir(parents=True)
    (cache / ".gitignore").write_text("*\n!.gitignore\n")
    (cache / ("a" * 64 + ".tar.gz")).write_bytes(b"cached upstream payload")
    (cache / "partial").mkdir()
    (cache / "partial/upstream.tar.gz").write_bytes(b"incomplete archive")
    (cache / "partial/.gitignore").write_bytes(b"nested cache file, not the build-directory marker")
    (hermes / "Dockerfile").write_text("fixture source")
    output = tmp_path / "source.zip"
    package.archive(output, [(path, str(path.relative_to(tmp_path))) for path in package.files(hermes)])
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert names == {"hermes/Dockerfile", "hermes/.upstream-cache/.gitignore", "MANIFEST.sha256.json"}
        destination = tmp_path / "unpacked"
        archive.extractall(destination)
    assert (destination / "hermes/.upstream-cache").is_dir()
    assert list((destination / "hermes/.upstream-cache").iterdir()) == [
        destination / "hermes/.upstream-cache/.gitignore"]

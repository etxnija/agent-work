from pathlib import Path

from bootstrap.bootstrap import COVERAGERC_TEMPLATE, bootstrap


class TestBootstrapCoveragerc:
    def test_python_project_gets_coveragerc(self, tmp_path: Path) -> None:
        bootstrap(tmp_path, "python")

        coveragerc = tmp_path / ".coveragerc"
        assert coveragerc.exists()
        assert coveragerc.read_text() == COVERAGERC_TEMPLATE

    def test_non_python_project_gets_no_coveragerc(self, tmp_path: Path) -> None:
        bootstrap(tmp_path, "go")

        assert not (tmp_path / ".coveragerc").exists()

    def test_existing_coveragerc_is_not_clobbered(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        coveragerc = tmp_path / ".coveragerc"
        coveragerc.write_text("[run]\nsource = mypkg\n")

        bootstrap(tmp_path, "python")

        assert coveragerc.read_text() == "[run]\nsource = mypkg\n"

from pathlib import Path

from bootstrap.bootstrap import CONCEPTS_INDEX_TEMPLATE, COVERAGERC_TEMPLATE, bootstrap


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


class TestBootstrapAgentFiles:
    def test_all_agent_files_copied(self, tmp_path: Path) -> None:
        bootstrap(tmp_path, "python")

        for name in ["planner.md", "reviewer.md", "refactor.md", "architect.md"]:
            assert (tmp_path / "agents" / name).exists()
            assert (tmp_path / ".claude" / "agents" / name).exists()

    def test_existing_agent_file_is_not_clobbered(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "agents").mkdir()
        planner = tmp_path / "agents" / "planner.md"
        planner.write_text("custom planner content\n")

        bootstrap(tmp_path, "python")

        assert planner.read_text() == "custom planner content\n"


class TestBootstrapMemoryModel:
    def test_concepts_index_created(self, tmp_path: Path) -> None:
        bootstrap(tmp_path, "python")

        concepts_index = tmp_path / "memory" / "concepts" / "index.md"
        assert concepts_index.exists()
        assert concepts_index.read_text() == CONCEPTS_INDEX_TEMPLATE

    def test_adr_dir_created_empty(self, tmp_path: Path) -> None:
        bootstrap(tmp_path, "python")

        adr_dir = tmp_path / "docs" / "arch" / "adr"
        assert adr_dir.is_dir()
        assert list(adr_dir.iterdir()) == []

    def test_agents_md_contains_memory_model_sections(self, tmp_path: Path) -> None:
        bootstrap(tmp_path, "python")

        agents_md = (tmp_path / "AGENTS.md").read_text()
        assert "## Memory model" in agents_md
        assert "## ADR conventions" in agents_md

    def test_existing_concepts_index_is_not_clobbered(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "memory" / "concepts").mkdir(parents=True)
        concepts_index = tmp_path / "memory" / "concepts" / "index.md"
        concepts_index.write_text("custom concepts index\n")

        bootstrap(tmp_path, "python")

        assert concepts_index.read_text() == "custom concepts index\n"

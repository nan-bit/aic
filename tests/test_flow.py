import pytest
import os
import sqlite3
from click.testing import CliRunner
from aic.cli import cli, index
from aic.db import get_node, get_dependencies

@pytest.fixture
def runner():
    return CliRunner()

@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    """Sets up a temp workspace and changes directory to it."""
    # Copy necessary files or just play in tmp_path
    monkeypatch.chdir(tmp_path)
    return tmp_path

def test_index_flow(runner, temp_workspace):
    # 1. Create files
    (temp_workspace / "db.py").write_text("def save(): pass", encoding="utf-8")
    (temp_workspace / "main.py").write_text("import db\ndef run(): db.save()", encoding="utf-8")
    
    # 2. Run index
    result = runner.invoke(cli, ["index"])
    assert result.exit_code == 0
    assert "Indexed: db.py" in result.output
    assert "Indexed: main.py" in result.output
    
    # 3. Verify DB content
    # We need to access the DB in the temp workspace
    # .aic/graph.db should exist
    assert (temp_workspace / ".aic/graph.db").exists()
    
def test_idempotency(runner, temp_workspace):
    (temp_workspace / "a.py").write_text("x=1", encoding="utf-8")
    
    # First run
    runner.invoke(cli, ["index"])
    
    # Second run
    result = runner.invoke(cli, ["index"])
    assert result.exit_code == 0
    # Should not index anything
    assert "Indexed: a.py" not in result.output

def test_context_command(runner, temp_workspace):
    (temp_workspace / "lib.py").write_text("def help(): pass", encoding="utf-8")
    (temp_workspace / "app.py").write_text("import lib", encoding="utf-8")
    
    runner.invoke(cli, ["index"])
    
    result = runner.invoke(cli, ["context", "app.py"])
    assert result.exit_code == 0
    assert "# Context for app.py" in result.output
    assert "## Dependencies" in result.output
    assert "### lib.py" in result.output

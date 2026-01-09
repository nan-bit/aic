# AIC: AI Compiler CLI

AIC (`aic`) is a semantic dependency graph manager designed to help AI agents understand and navigate your codebase efficiently. It "compiles" your code into **Rich Skeletons**—lightweight, token-efficient representations that preserve signatures, docstrings, and side-effects while discarding implementation details.

## Why AIC?

When working with Large Language Models (LLMs) on brownfield applications, context is expensive. Feeding raw source code wastes tokens on implementation logic when the AI often just needs the "API Contract" and dependency graph.

**AIC solves this by:**
- **Reducing Token Usage**: Compresses files into skeletons (often 90% smaller than source).
- **Preserving Intent**: Keeps docstrings, type hints, and critical effects (e.g., `RAISES`, `CALLS`).
- **Mapping Dependencies**: Automatically tracks internal imports to help agents trace logic across files.
- **Fast & Local**: Uses strict static analysis (AST) with zero AI latency for indexing.

## Installation

### Prerequisites
- Python 3.8+

### Install from Source

 Clone the repository and install in editable mode:

```bash
git clone https://github.com/your-org/aic.git
cd aic
pip install -e .
```

*Note: If you encounter build issues with `hatchling`, this project uses `setuptools` fallback. Ensure you have `setuptools>=61.0` installed.*

## Usage

### 1. Index Your Codebase
Run the indexer from the root of your project. This will scan for Python files, generate skeletons, and build the dependency graph in `.aic/graph.db`.

```bash
aic index
```

**Output:**
```text
Indexed: src/main.py
Indexed: src/utils/db.py
Finished indexing. Processed 12 files.
```

### 2. Retrieve Context for an Agent
When an agent needs to understand a file, fetch its "Rich Skeleton" plus the skeletons of its immediate dependencies.

```bash
aic context <filepath>
```

**Example:**
```bash
aic context src/main.py
```

**Output (Markdown):**
```markdown
# Context for src/main.py
def process_data(data) -> bool:
    """Validates and saves data."""
    # CALLS: validate, save | RAISES: ValueError | RETURNS: value

## Dependencies
### src/utils/db.py
def save(record):
    """Writes record to database."""
    # CALLS: execute, commit
```

## Technical Architecture

AIC is built on three core pillars: Deterministic Indexing, Relational Storage, and Rich Skeletonization.

### 1. Database Schema (`.aic/graph.db`)
We use SQLite to store the dependency graph, ensuring the index is portable and zero-dependency.

*   **`nodes` table**: Stores the state of each file.
    *   `path` (PK): Relative file path.
    *   `hash`: SHA256 of the *source code* to detect changes.
    *   `skeleton`: The compressed "Rich Skeleton" (context for LLMs).
    *   `status`: `CLEAN` | `DIRTY` (Dirty = dependencies changed).
*   **`edges` table**: Represents the dependency graph.
    *   `source`: File importing the dependency.
    *   `target`: The dependency file being imported.

### 2. The "Rich Skeleton" Indexer
The indexer uses Python's `ast` module to strip implementation details while preserving the "API Contract".

**Key Extraction Logic:**
*   **Signatures**: Preserves function/class definitions and type hints (reconstructed for clarity).
*   **Docstrings**: Keeps 100% of docstrings to capture intent.
*   **Effect Analysis**: Analyzes function bodies to detect:
    *   `RAISES`: Exceptions raised.
    *   `CALLS`: External functions called (top 5).
    *   `RETURNS`: Specific return values (e.g., `None`, `<variable>`, `True`) or expressions.
*   **Compression**: Logic bodies are replaced with summary comments (e.g., `# CALLS: db.save | RAISES: ValueError`), typically reducing context size by 90%+.

### 3. Workflow
1.  **MapReduce Indexing**: `aic index` scans files, computes a diff based on SHA256 hashes, and only re-processes changed files.
2.  **Propagated Updates**: When a file changes, its status is updated, and we can identify dependent nodes (reverse edges) that may need re-contextualization (future feature).
3.  **Context Retrieval**: `aic context` creates a concatenated view of the target file's skeleton + its immediate dependencies' skeletons.

## Development

### Project Structure
- `aic/skeleton.py`: The `RichSkeletonizer` AST visitor.
- `aic/db.py`: SQLite database operations.
- `aic/cli.py`: CLI entry point and orchestration.

### Running Tests
To run the test suite, install the test dependencies and run `pytest`:

```bash
pip install -e ".[test]"
PYTHONPATH=. pytest tests/
```

## Acknowledgements

This project was inspired by ideas from [@rodydavis](https://github.com/rodydavis). For more information, please refer to the following patent: [System and method for semantic dependency graph management](https://www.tdcommons.org/dpubs_series/8241/).

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

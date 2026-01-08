import click
import os
import ast
from pathlib import Path
from .db import init_db, upsert_node, update_edges, get_node, get_dependencies, mark_dirty
from .skeleton import RichSkeletonizer
from .utils import calculate_file_hash

@click.group()
def cli():
    """AI Compiler CLI"""
    pass

@cli.command()
def index():
    """Index the current directory."""
    init_db()
    count = 0
    
    # Walk directory
    for root, dirs, files in os.walk("."):
        # Ignore .aic and __pycache__
        if ".aic" in dirs:
            dirs.remove(".aic")
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        if ".git" in dirs:
            dirs.remove(".git")
            
        for file in files:
            if not file.endswith(".py"):
                continue
                
            file_path = Path(root) / file
            rel_path = str(file_path)
            
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            current_hash = calculate_file_hash(content)
            
            # Check DB
            node = get_node(rel_path)
            if node and node[1] == current_hash:
                continue # Unchanged
                
            # Changed or New
            try:
                tree = ast.parse(content)
                visitor = RichSkeletonizer()
                visitor.visit(tree)
                skeleton = "\n".join(visitor.output)
                dependencies = visitor.dependencies
                
                # Resolve dependencies
                resolved_deps = []
                file_dir = Path(root)
                
                for dep in dependencies:
                    name = dep['name']
                    level = dep['level']
                    
                    target_path = None
                    
                    if level > 0:
                        # Relative import
                        # level 1 = ., level 2 = ..
                        base = file_dir
                        for _ in range(level - 1):
                            base = base.parent
                        
                        if name:
                            # from .module import ...
                            # Check for module.py or module/__init__.py
                            dep_rel_path = name.replace('.', '/')
                            candidates = [
                                base / (dep_rel_path + ".py"),
                                base / dep_rel_path / "__init__.py"
                            ]
                        else:
                            # from . import ... (imports from __init__.py of current package)
                            candidates = [base / "__init__.py"]
                            
                        for cand in candidates:
                            if cand.exists():
                                target_path = cand
                                break
                    else:
                        # Absolute import
                        # Try to find it in the current project root (simple heuristic for now)
                        # We assume project root is current working directory "."
                        dep_rel_path = name.replace('.', '/')
                        candidates = [
                            Path(dep_rel_path + ".py"),
                            Path(dep_rel_path) / "__init__.py"
                        ]
                        for cand in candidates:
                            if cand.exists():
                                target_path = cand
                                break
                                
                    if target_path:
                        # Normalize to string
                        resolved_deps.append(str(target_path))
                    else:
                        # Keep original name if not found locally (external dependency)
                        # But we only want to track edges to existing nodes?
                        # For now, let's just ignore external ones for the graph edges to keep it clean,
                        # OR keep them to show external deps.
                        # Prompt says "Edges... source... target".
                        # If I add external deps, I should probably mark them as such?
                        # Let's just skip them for now to focus on internal graph.
                        pass
                
                upsert_node(rel_path, current_hash, skeleton)
                update_edges(rel_path, resolved_deps)
                mark_dirty(rel_path)
                
                click.echo(f"Indexed: {rel_path}")
                count += 1
            except Exception as e:
                click.echo(f"Error parsing {rel_path}: {e}", err=True)
                
    click.echo(f"Finished indexing. Processed {count} files.")

@cli.command()
@click.argument("filepath")
def context(filepath):
    """Retrieve context for a file."""
    # Normalize path
    filepath = str(Path(filepath)) # Handle ./ prefix if needed, but simple for now
    if filepath.startswith("./"):
        filepath = filepath[2:]
        
    node = get_node(filepath)
    if not node:
        click.echo(f"File not found in index: {filepath}", err=True)
        return
        
    skeleton, _, _ = node
    
    click.echo(f"# Context for {filepath}")
    click.echo(skeleton)
    click.echo("\n## Dependencies")
    
    deps = get_dependencies(filepath)
    for dep in deps:
        dep_node = get_node(dep)
        if dep_node:
            click.echo(f"### {dep}")
            click.echo(dep_node[0])
        else:
            click.echo(f"### {dep} (Not indexed or external)")

if __name__ == "__main__":
    cli()

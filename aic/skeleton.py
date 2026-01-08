import ast

class RichSkeletonizer(ast.NodeVisitor):
    def __init__(self):
        self.output = []
        self.indent_level = 0
        self.dependencies = []

    def _indent(self):
        return "    " * self.indent_level

    def visit_Import(self, node):
        for alias in node.names:
            self.output.append(f"import {alias.name}")
            self.dependencies.append({'name': alias.name, 'level': 0})
    
    def visit_ImportFrom(self, node):
        names = ", ".join(n.name for n in node.names)
        self.output.append(f"from {node.module} import {names}")
        if node.module:
            self.dependencies.append({'name': node.module, 'level': node.level})
        elif node.level > 0:
             # from . import foo -> module is None, foo in names
             for alias in node.names:
                 self.dependencies.append({'name': alias.name, 'level': node.level})

    def visit_ClassDef(self, node):
        self.output.append(f"\n{self._indent()}class {node.name}:")
        self.indent_level += 1
        if (doc := ast.get_docstring(node)):
            self.output.append(f'{self._indent()}"""{doc}"""')
        self.generic_visit(node)
        self.indent_level -= 1

    def visit_FunctionDef(self, node):
        self._handle_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._handle_func(node, is_async=True)

    def _handle_func(self, node, is_async=False):
        # 1. Signature
        args = [a.arg for a in node.args.args]
        prefix = "async def" if is_async else "def"
        sig = f"{self._indent()}{prefix} {node.name}({', '.join(args)})"
        
        # Enhanced type hint extraction
        if node.returns:
            # Try to get simple names/values, fallback to ...
            if isinstance(node.returns, ast.Name):
                sig += f" -> {node.returns.id}"
            elif isinstance(node.returns, ast.Constant):
                sig += f" -> {node.returns.value}"
            elif isinstance(node.returns, ast.Subscript):
                 # Handle List[int] etc roughly if possible, or just ...
                try:
                    # Python 3.9+ has ast.unparse, but we target 3.8+
                    # For now, let's just mark complex types as ... or try to handle simple generics
                    if isinstance(node.returns.value, ast.Name):
                        sig += f" -> {node.returns.value.id}[...]"
                    else:
                         sig += " -> ..."
                except:
                    sig += " -> ..."
            else:
                 sig += " -> ..."
        
        self.output.append(f"{sig}:")
        
        self.indent_level += 1
        
        # 2. Docstring
        if (doc := ast.get_docstring(node)):
            self.output.append(f'{self._indent()}"""{doc}"""')

        # 3. Effects Analysis (IO/Returns/Raises)
        returns = set()
        raises = set()
        calls = set()

        # Walk only the body of this function
        for child in node.body:
            for subnode in ast.walk(child):
                # Capture Returns
                if isinstance(subnode, ast.Return):
                    if subnode.value is None:
                        returns.add("None")
                    elif isinstance(subnode.value, ast.Constant):
                        returns.add(repr(subnode.value.value))
                    elif isinstance(subnode.value, ast.Name):
                        returns.add(f"<{subnode.value.id}>")
                    else:
                        returns.add("<expression>")
                
                # Capture Raises
                elif isinstance(subnode, ast.Raise):
                    if isinstance(subnode.exc, ast.Call) and isinstance(subnode.exc.func, ast.Name):
                        raises.add(subnode.exc.func.id)
                    elif isinstance(subnode.exc, ast.Name):
                        raises.add(subnode.exc.id)

                # Capture Function Calls
                elif isinstance(subnode, ast.Call):
                    if isinstance(subnode.func, ast.Name):
                        calls.add(subnode.func.id)
                    elif isinstance(subnode.func, ast.Attribute):
                        calls.add(subnode.func.attr)

        # 4. Generate Summary
        meta = []
        if raises: meta.append(f"RAISES: {', '.join(sorted(raises))}")
        if calls: meta.append(f"CALLS: {', '.join(sorted(list(calls))[:5])}")
        if returns: meta.append(f"RETURNS: {', '.join(sorted(returns))}")
        
        if meta:
            self.output.append(f"{self._indent()}# {' | '.join(meta)}")
        elif not doc:
            self.output.append(f"{self._indent()}...")
            
        self.indent_level -= 1

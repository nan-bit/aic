"""Ways to ask the engine a question.

Both surfaces are thin: they call aic/query.py and render what comes back.
Neither holds analysis logic, which is what lets them differ so much --

  cli  a human, one question per process, no dependencies, Python 3.9+
  mcp  a coding agent, many questions per session, ranked and token-budgeted

Both are kept on purpose. The CLI is how the claim gets checked without
standing up a protocol client, it is the debugging surface when the server
misbehaves, and it keeps the dependency-free path intact -- the MCP SDK needs
3.10+ and pulls 27 transitive dependencies that `aic` itself does not.
"""

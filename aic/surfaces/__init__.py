"""Ways to ask the engine a question.

Both surfaces are thin: they call aic/query.py and render what comes back.
Neither holds analysis logic, which is what lets them differ so much --

  cli  a human, one question per process, no dependencies, Python 3.9+
  mcp  a coding agent, many questions per session, ranked and token-budgeted

See DESIGN.md, "Two surfaces, one query layer", for why both exist.
"""

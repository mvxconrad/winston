"""Winston's brain — everything LLM-related and AI-adjacent.

This package owns:
  - client.py : Ollama HTTP client (sync + async background worker)
  - prompt.py : observation-snapshot builder for LLM context
  - history.py (future) : CSV-aware trend/baseline computation
  - tools.py (future) : tool definitions the model can call
  - agent.py (future) : the orchestrator combining the above

The split between `brain/` and `panels/` is deliberate:
  - panels/ = things that render in the dashboard
  - brain/  = things that think about what the panels are seeing

A panel can import from brain (the COMMENTARY panel does, to display LLM
output). Brain code never imports panel rendering — just panel DATA via
the llm_summary() interface.
"""
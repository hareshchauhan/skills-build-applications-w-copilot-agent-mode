# Legacy / quarantined modules

These files reference symbols (`FNOLPayload`, `BaseAgent`, `FNOLIntakeAgent`,
`MaturityLevel`, `ENGINE_VERSION`, etc.) that **no longer exist** in
`fnol_workflow_engine`. They were a parallel class-based design that diverged
from the canonical `stage_*` function-based workflow engine and cannot import.

Quarantined here in 2026 during the HIGH-severity review to remove broken
import paths from the main package. Restore individually if/when the symbols
they need are reintroduced.

| File                    | Imports it expects                                              |
|-------------------------|------------------------------------------------------------------|
| `fnol_tools_registry.py`| `FNOLPayload`, `TelematicsSignal`                                |
| `fnol_tools_langchain.py`| (depends on `fnol_tools_registry`)                              |
| `fnol_tools_mcp.py`     | (depends on `fnol_tools_registry`)                               |
| `fnol_l3_agents.py`     | `BaseAgent`, `FNOLIntakeAgent`, `TriageAssignmentAgent`, …       |
| `fnol_agents_ext.py`    | `BaseAgent`, `FraudSignalDetectionAgent`                         |
| `coverage_agent.py`     | `BaseAgent`                                                      |
| `fnol_maturity.py`      | `MaturityLevel`                                                  |

# Changelog

## [0.1.2](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/claude-agent-sdk-macp-v0.1.1...claude-agent-sdk-macp-v0.1.2) (2026-10-26)


### Bug Fixes

* **deps:** allow macp-sdk-python 0.21 in the LangGraph and Claude Agent SDK adapters ([a6bbab5](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/a6bbab50d9dfc5969df594ad8d993d6a5cc2d1f6))

## [0.1.1](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/claude-agent-sdk-macp-v0.1.0...claude-agent-sdk-macp-v0.1.1) (2026-10-26)


### Bug Fixes

* **deps:** allow macp-sdk-python 0.20 in the LangGraph and Claude Agent SDK adapters ([#196](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/issues/196)) ([2ecf0e1](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/2ecf0e1d60dd76c8c154312f75bc961bac85ed9a))

## 0.1.0 (2026-10-25)


### Features

* **claude-agent-sdk:** claude-agent-sdk-macp — Claude Agent SDK subagents vote in a binding MACP Decision or Quorum session. `macp_consensus` turns `AgentDefinition`s into participants and returns the Commitment; `macp_consensus_server` / `consensus_tool` expose it to a lead agent as `mcp__macp__consensus`; `claude_evaluator` is the Claude-backed `EvaluationStrategy`; `consensus_receipt` builds an RFC-MACP-0013 decision receipt ([#190](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/pull/190))

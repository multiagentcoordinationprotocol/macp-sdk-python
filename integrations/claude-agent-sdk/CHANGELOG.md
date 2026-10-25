# Changelog

## 0.1.0 (2026-10-25)


### Features

* **claude-agent-sdk:** claude-agent-sdk-macp — Claude Agent SDK subagents vote in a binding MACP Decision or Quorum session. `macp_consensus` turns `AgentDefinition`s into participants and returns the Commitment; `macp_consensus_server` / `consensus_tool` expose it to a lead agent as `mcp__macp__consensus`; `claude_evaluator` is the Claude-backed `EvaluationStrategy`; `consensus_receipt` builds an RFC-MACP-0013 decision receipt ([#190](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/pull/190))

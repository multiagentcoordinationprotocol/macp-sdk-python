# Changelog

## [0.1.3](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/langgraph-macp-v0.1.2...langgraph-macp-v0.1.3) (2026-10-26)


### Bug Fixes

* **deps:** allow macp-sdk-python 0.22 in the LangGraph and Claude Agent SDK adapters ([cfed165](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/cfed165790b8f0c349f55c965cf9a04a3eae62b8))

## [0.1.2](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/langgraph-macp-v0.1.1...langgraph-macp-v0.1.2) (2026-10-26)


### Bug Fixes

* **deps:** allow macp-sdk-python 0.21 in the LangGraph and Claude Agent SDK adapters ([a6bbab5](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/a6bbab50d9dfc5969df594ad8d993d6a5cc2d1f6))

## [0.1.1](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/compare/langgraph-macp-v0.1.0...langgraph-macp-v0.1.1) (2026-10-26)


### Bug Fixes

* **deps:** allow macp-sdk-python 0.20 in the LangGraph and Claude Agent SDK adapters ([#196](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/issues/196)) ([2ecf0e1](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/commit/2ecf0e1d60dd76c8c154312f75bc961bac85ed9a))

## 0.1.0 (2026-10-25)

First release.

### Features

* `MacpConsensusNode`: a LangGraph node that settles a binding MACP Decision or Quorum session
  and writes `{outcome, session_id, commitment_hash, mode, commitment, approvals, threshold}` to
  graph state ([#186](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/pull/186))
* `consensus_receipt()`: an RFC-MACP-0013 decision receipt in one call
* `llm_evaluator()`: any LangChain chat model as a participant's evaluator

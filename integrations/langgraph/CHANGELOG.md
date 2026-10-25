# Changelog

## 0.1.0 (2026-10-25)

First release.

### Features

* `MacpConsensusNode`: a LangGraph node that settles a binding MACP Decision or Quorum session
  and writes `{outcome, session_id, commitment_hash, mode, commitment, approvals, threshold}` to
  graph state ([#186](https://github.com/multiagentcoordinationprotocol/macp-sdk-python/pull/186))
* `consensus_receipt()`: an RFC-MACP-0013 decision receipt in one call
* `llm_evaluator()`: any LangChain chat model as a participant's evaluator

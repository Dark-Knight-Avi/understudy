# Architecture Decision Records

Each ADR records one decision: the context that forced it, what we chose, what that costs us, and
what we rejected. They exist so that in six months nobody re-litigates a settled question from
scratch — and so that when a decision *should* be revisited, the original reasoning is available to
argue against.

**Conventions**

- ADRs are immutable once accepted. To change a decision, write a new ADR and mark the old one
  `Superseded by ADR-NNNN`. Never delete or rewrite one.
- Status is one of: `Proposed`, `Accepted`, `Superseded by ADR-NNNN`, `Amended by ADR-NNNN`.
- Every ADR names its alternatives. A decision with no rejected options was not a decision.

| ADR | Status | Decision |
|---|---|---|
| [0001](./0001-partition-by-service.md) | Accepted | Partition the fleet by service; do not cluster the GPUs |
| [0002](./0002-assemble-vs-build.md) | Accepted | Build three components; assemble everything else from open source |
| [0003](./0003-model-tiers-and-ladder.md) | Accepted | Two quality tiers, and an elastic model ladder driven by measured free VRAM |
| [0004](./0004-egress-policy.md) | Accepted | Search queries may leave the network; documents and code never do |
| [0005](./0005-rag-as-a-model-endpoint.md) | Accepted | Expose the RAG service as an OpenAI-compatible model, not as a chat-UI plugin |

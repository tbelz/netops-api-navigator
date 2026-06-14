# Runtime Hydration Roadmap

Status: roadmap

This roadmap sketches a multi-PR path for adding an opt-in runtime hydration
layer to the MCP server. It intentionally stays high-level: each stage should
get its own focused plan before implementation.

## Goal

Build a generic, graph-backed hydration substrate on top of the existing v2 API
graph. An agent should be able to hydrate live Central state for the endpoint or
investigation it is working on without the repository predefining every possible
Central "aspect" by hand.

The long-term system should answer three questions:

1. What can be fetched? The existing v2 API graph answers this.
2. What has been fetched? Hydrator runs and observations answer this.
3. How did this runtime fact get into the graph? Provenance and freshness answer
   this.

## Guiding Principles

- Keep v2 as the API knowledge graph foundation. Do not create
  `MCP_KNOWLEDGE_PROJECTION=v3` just to enable runtime hydration.
- Gate runtime hydration independently with an opt-in flag such as
  `MCP_RUNTIME_HYDRATION=true`.
- Make the first-class abstraction generic endpoint hydration, not a curated
  list of hand-written Central aspects.
- Preserve raw observations before promoting facts into typed runtime nodes.
- Promote common typed facts only when they are useful, stable, and backed by
  provenance to the raw observation and source API.
- Treat typed runtime graph tables as highways, not as the source of truth.
- Prefer visible degradation over silent loss: unsupported endpoints, ambiguous
  identity, missing required params, pagination uncertainty, or stale facts must
  be explicit.

## Relationship To The Existing API Graph

The existing v2 graph remains the contract graph:

```text
ApiEndpoint -> Parameter / RequestBody / Response / SchemaComponent / Property
```

Runtime hydration should attach to it through run/provenance nodes rather than a
direct-only `RuntimeNode -> ApiEndpoint` edge:

```text
ApiEndpoint
  <-[:CALLED_API]-
HydrationRun
  -[:PRODUCED_OBSERVATION]->
RuntimeObservation
  -[:OBSERVATION_MATERIALIZED_FACT]->
RuntimeFact or typed runtime node
```

This preserves the important context that a direct edge cannot carry: call
parameters, scope, timestamp, pagination, success/failure state, response hash,
source endpoint, and freshness.

## Roadmap

Implementation PRs may bundle adjacent roadmap sections when the review cycle
cost is high. The important boundary is behavioral maturity: each PR should
ship a coherent, tested slice that keeps runtime hydration disabled unless
`MCP_RUNTIME_HYDRATION=true` is set.

### PR 1: Roadmap and terminology

- Add this roadmap and align documentation vocabulary around runtime hydration,
  hydrator runs, observations, materialized facts, provenance, and freshness.
- Record the decision that runtime hydration is a separate opt-in capability
  from `MCP_KNOWLEDGE_PROJECTION=v2`.
- Explicitly reject a purely artisanal roadmap where each useful Central domain
  must be pre-modeled before agents can hydrate it.

### PR 2: Feature flag and inert runtime-hydration shell

- Add a disabled-by-default setting such as `MCP_RUNTIME_HYDRATION`.
- When disabled, no hydration tools are registered and current server behavior
  remains unchanged.
- When enabled, register only introspection/no-op surfaces at first, enough to
  prove the flag, docs, startup logging, and tests.

### PR 3: Generic hydration metadata schema

- Add graph schema for hydration runs, runtime observations, and provenance
  links back to `ApiEndpoint`.
- Store enough metadata to answer: which endpoint was called, with which params,
  at what time, under which run, with what status, and what response hash.
- Keep the schema generic; do not add one table per Central endpoint.

### PR 4: Endpoint hydration capability classifier

- Build a deterministic classifier over the v2 API graph that identifies
  read-capable endpoint candidates.
- Classify GET endpoints by practical execution shape: list vs item lookup,
  required parameters, path parameters, known/unknown pagination, response root,
  likely item identity fields, and expected cost.
- Surface unsupported or ambiguous endpoints as explicit capability gaps rather
  than hiding them.

PRs 2-4 are safe to land together as the first implementation slice because
they do not execute live API calls or persist runtime observations. Together
they establish the flag, the generic observation schema, and graph-backed
planning metadata that later executor work can rely on.

### PR 5: Generic endpoint hydration executor

- Add the first real hydration tool behind the flag. It should hydrate a chosen
  read endpoint by method/path or endpoint id plus supplied parameters.
- Reuse existing Central/GreenLake HTTP clients and graph-backed validation
  where possible.
- Persist raw observations and run provenance for successful calls.
- Refuse writes; the first hydration executor is GET/read-only only.
- Start with bounded, conservative pagination behavior. Unknown pagination must
  be reported, not guessed silently.

### PR 6: Observation query and freshness UX

- Add agent-facing ways to find hydrated observations for an endpoint, scope, or
  investigation.
- Add freshness metadata and stale/missing-state reporting so agents can decide
  whether to hydrate before answering.
- Keep generic observation access useful even before typed materialization exists.

PRs 5-6 are also good candidates to land together. The executor is only useful
if an agent can immediately inspect what was observed, while the observation
query tools stay safe because they read already-persisted data and do not call
live APIs.

### PR 7: Generic materialization layer

- Add a deterministic materialization path from observations into queryable
  runtime facts.
- Materialization should use response schemas and observed data to create stable,
  provenance-backed graph facts without requiring a bespoke table for every API.
- If identity or relationship inference is ambiguous, keep the data as raw
  observations and expose the ambiguity.

### PR 8: Promote high-value typed highways carefully

- Promote selected runtime facts into typed tables or relationships only after
  the generic observation/materialization layer proves useful.
- Existing seed-backed concepts such as devices, sites, topology, config
  profiles, ports, clients, and radios can become typed highways, but they are
  not the foundation of the system.
- Every promoted fact must remain traceable to `HydrationRun`, `RuntimeObservation`,
  and `ApiEndpoint`.

PR 7 should land before any typed highway work. The generic fact layer is the
anti-artisanal checkpoint: if an observed object has clear identity, it should
be queryable as a `RuntimeFact` even when no Central-specific typed table exists
yet. Typed highways in PR 8 are optimization paths, not prerequisites for use.

### PR 9: Agent planning helpers

- Add higher-level helpers that let an agent ask what needs to be hydrated for
  a question, which endpoints can supply it, what cost/freshness tradeoff is
  involved, and what is already available.
- Keep these helpers advisory at first; they should explain the plan before
  running expensive hydration.

### PR 10: Multi-provider readiness

- Generalize provider boundaries so Central, GreenLake, and future network
  management APIs can share the hydration substrate.
- Provider-specific logic should live in endpoint classifiers, auth clients, or
  optional materialization rules, not in the core observation/provenance model.

## Non-Goals

- Do not create a manually curated hydrator for every one of the Central APIs.
- Do not make typed runtime tables the only way to use hydrated data.
- Do not expose mutating endpoint hydration in the first roadmap cycle.
- Do not silently infer primary keys, relationships, or deletion semantics when
  the API graph and observed response data are ambiguous.

## Definition Of Done For The Roadmap

The roadmap is complete when the server can, behind an explicit opt-in flag:

- discover read-capable endpoints from the v2 API graph;
- hydrate arbitrary supported read endpoints with supplied parameters;
- persist raw observations with run metadata, source API provenance, response
  hashes, timestamps, and status;
- expose stale/missing hydration state to agents;
- materialize useful runtime facts generically when identity is clear;
- preserve provenance from materialized facts back to observations, runs, and
  API endpoints;
- keep current graph-first API discovery and existing MCP behavior unchanged
  when hydration is disabled;
- provide enough tests and smoke coverage to prove the hydration substrate works
  across multiple endpoint shapes, not only a hand-picked domain.

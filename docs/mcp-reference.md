# MCP reference

[Documentation index](README.md) · [Profiles and settings](configuration.md)

This is an overview of the current source's MCP surface. The installed server's
`tools/list` response provides exact input schemas and descriptions. Availability
depends on the selected profile, credentials, and optional feature flags.

## Graph and discovery

These tools query local data; live topology is populated separately by seeds or
hydration. An empty result is not evidence that a network is healthy or has no
matching objects.

| Tool | Availability | Purpose |
|---|---|---|
| `query_fts` | Both profiles | Keyword search across endpoint, property, documentation, script, and runtime indexes |
| `query_api_schema` | Both profiles | Traverse endpoints, parameters, request/response schemas, and properties |
| `query_yang` | Both profiles | Map YANG paths, CLI commands, and configuration concepts to the API graph |
| `query_topology` | Both profiles | Query organizations, sites, devices, groups, and neighbor links |
| `query_graph` | Both profiles | General read-only Cypher across the graph |
| `get_raw_schema` | Both profiles | Read raw OpenAPI JSON for known schema component IDs |
| `query_runtime` | Full profile | Query stored runtime observations, facts, and entities |
| `write_graph` | Full profile | Mutate local graph data; not a cloud API call |
| `get_server_status` | Both profiles | Inspect connection, profile, graph, and topology synchronization status |

The `query_*` tools share a read-only Cypher executor. A `queries` batch contains
items with `cypher`, optional `parameters`, and optional `label`; it takes
precedence over single-query inputs. Items run sequentially and failures are
reported per item. `get_raw_schema` also supports component-ID batches; use its
advertised input schema rather than the Cypher batch shape.

Responses have row and byte limits. Oversized values may be replaced by
truncation envelopes; aggregate limits can omit trailing batch results. Inspect
those indicators before treating a result as complete. Use narrower queries or
structured schema traversal when a raw schema is too large. See
[response limits](configuration.md#graph-response-limits).

Volatile topology fields can carry age warnings. A query reads stored state;
it does not automatically refresh that state from Central.

## Live API calls

| Tool | Availability | Purpose |
|---|---|---|
| `call_central_api` | Valid Central credentials, either profile | Authenticated Central request with graph-backed pre-flight validation; GET-only in workshop |
| `paginate_central_api` | Connected workshop | Bounded GET collection retrieval with cursor/offset pagination |
| `call_greenlake_api` | Full profile and valid effective GreenLake credentials | Authenticated GreenLake request with pre-flight validation |

Validation checks the discovered endpoint, parameters, and body shape. It does
not guarantee that the upstream API will accept the request or that the account
has permission. Read-only mode restricts cloud methods; see
[profile behavior](configuration.md#profiles-and-connectivity).

## Scripts

| Tool | Availability | Purpose |
|---|---|---|
| `list_scripts` | Full profile | List saved and bundled scripts, optionally by tag |
| `get_script_content` | Full profile | Read a script and its metadata |
| `save_script` | Full profile | Save/update a Python script and metadata |
| `execute_script` | Connected full profile | Run a saved script with authenticated helpers |

Discovery mode supports authoring and saving scripts but does not register
execution. Workshop mode omits all four tools. In the full profile,
`--read-only` does not remove script execution or local graph mutation.

The helper API and execution model are explained in the
[technical overview](TECHNICAL_OVERVIEW.md#script-execution).
Use `docs://script-writing-guide` for instructions in an MCP client.

## Optional features

### Compiler diagnostics

Set `MCP_COMPILER_TOOLS=true` in the full profile. These tools require the
compiler projection and AST sidecars:

| Tool | Purpose |
|---|---|
| `get_openapi_source_detail` | Resolve a projection row to provenance, AST metadata, and original OpenAPI source |
| `get_compiler_graph_health` | Run bounded traversal-health samples against compiler artifacts |

Endpoint and schema discovery still use the graph tools. The
[compiler tooling proposal](compiler-mcp-tooling-proposal.md) records design
context; it does not define the installed tool surface.

### Runtime hydration

Set `MCP_RUNTIME_HYDRATION=true` in the full profile. This opt-in surface supports
planning, bounded live observations, materialized facts, and generic entities.

| Tool | Purpose |
|---|---|
| `get_runtime_hydration_status` | Report stage, graph availability, provider readiness, and tables |
| `list_runtime_hydration_providers` | Inspect provider boundaries and client availability |
| `list_runtime_hydration_candidates` | Discover GET endpoints eligible for hydration planning |
| `plan_runtime_hydration` | Decide whether stored data, materialization, refresh, or parameters are needed |
| `hydrate_runtime_graph` | Plan and, for an exact target when needed, fetch a bounded GET, materialize facts, and promote entities; search-only and dry-run calls only plan |
| `hydrate_runtime_endpoint` | Execute a bounded GET and persist its observation; requires the provider client |
| `list_runtime_observations` | List stored observations |
| `get_runtime_observation` | Read an observation and its observed objects/fields |
| `get_runtime_hydration_state` | Inspect missing/fresh/stale/unknown endpoint state |
| `materialize_runtime_facts` | Promote observations with clear identities to facts |
| `list_runtime_facts` | Find facts by endpoint, entity type, or identity |
| `get_runtime_fact` | Read a fact and its provenance |
| `promote_runtime_entities` | Group clear-identity facts into provider-scoped entities |
| `list_runtime_entities` | Find entities by provider, endpoint, type, or identity |
| `get_runtime_entity` | Read an entity with fact-level provenance |

Stored-data tools do not imply that a live provider is connected. The
[hydration roadmap](runtime-hydration-roadmap.md) separates implemented stages
from future work.

## Resources and prompts

| Resource | Availability | Contents |
|---|---|---|
| `api://endpoint-catalog` | Both profiles | Compact API path tree, filtered by profile/read-only settings |
| `docs://endpoint-catalog` | Both profiles | Alias for the endpoint catalog |
| `graph://schema` | Both profiles | Live graph tables, relationships, counts, and query guidance |
| `graph://seed-status` | Both profiles | Background seed progress and errors |
| `docs://central/overview` | Full profile | Central API overview |
| `docs://script-writing-guide` | Full profile | Script-authoring guidance |
| `docs://config-workflows` | Full profile | Configuration workflow guidance |
| `script://seeds` | Full profile | Bundled automation script descriptions |
| `docs://vsg/list` | Full profile | Available Validated Solution Guide sections |
| `docs://vsg/{section_id}` | Full profile | One stored VSG section |

The full profile also registers `analyze_inventory`, `troubleshoot_device`,
`analyze_config`, and `write_script` prompt templates. Prompts guide a client;
they do not grant credentials or make otherwise unavailable tools callable.

## Discovery sequence

1. Read `api://endpoint-catalog` when the endpoint family is already known.
2. Use `query_fts` for a feature name, property, configuration concept, or CLI term.
3. Inspect exact parameters and request/response shapes with `query_api_schema`.
4. Use `query_yang` to follow CLI/YANG relationships when relevant.
5. Fetch a known component with `get_raw_schema` only when structured traversal
   omits necessary source detail.
6. With credentials configured, use the appropriate live API tool after checking
   method, path, parameters, and body.

Registration is controlled in [server.py](../src/netops_api_navigator/server.py).
Tool implementations and input descriptions live in
[tools/](../src/netops_api_navigator/tools/).

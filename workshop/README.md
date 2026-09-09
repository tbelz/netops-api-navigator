# NetOps API Navigator Workshop

This folder is the Docker-free starter for running NetOps API Navigator locally.
Credentials remain inside the local VS Code extension host. The fail-closed
`workshop` profile exposes API discovery, a locally synchronized live-topology
graph, and Central GET requests only. User-authored script execution, GreenLake
access, direct graph writes, and mutating API methods are disabled.

NetOps API Navigator is an independent community project. It is not affiliated
with, sponsored by, endorsed by, or supported by Hewlett Packard Enterprise.
HPE Aruba Networking Central and HPE GreenLake Platform are referenced solely
to describe compatibility. All trademarks belong to their respective owners.

## Supported platforms

- Windows 10/11 x64 without WSL or Docker
- macOS on Apple Silicon
- Linux x64
- VS Code with GitHub Copilot and MCP support enabled

## One-time preparation

1. Install or request access to VS Code and GitHub Copilot.
2. Install `uv`:
   - Windows PowerShell: `winget install --id=astral-sh.uv -e`
   - macOS with Homebrew: `brew install uv`
   - Linux: use the [official uv installer](https://docs.astral.sh/uv/)
3. Extract this release bundle and open the extracted folder in VS Code.
4. Verify the bundled installation and knowledge database before the workshop:

   ```text
   uv tool run --python 3.12 --no-build --constraints constraints.txt --no-index --find-links wheelhouse --from ./netops_api_navigator-0.3.0-py3-none-any.whl netops-api-navigator doctor --profile workshop --knowledge-release-tag knowledge-db-20260907-043531 --knowledge-sha256 c9730faf52ecb99b6409d8777814982d684ffed7c673c4a749842cb520c8a56e --skip-credentials
   ```

   `uv` installs Python 3.12 in the current user context when necessary. The
   server and all Python package dependencies are loaded from this bundle;
   administrator access, WSL, Docker, and PyPI access are not required. The
   initial Python download still requires access to the official uv-managed
   Python distribution unless Python 3.12 is already installed.

When VS Code asks for the Central API URL, copy the exact Base URL shown in
**Central → Menu → API Gateway → REST API**. The URL is account/cluster-specific;
do not reuse the URL from another Lab or tenant.

## Connect from VS Code

VS Code detects `.vscode/mcp.json`. On first start it prompts for the Central
API URL, client ID, and client secret. The secret is declared as a password
input and is never stored in a project file. Never copy credentials into source
code, dashboard files, issues, or chat output.

The configuration must run in the local VS Code extension host. A remote agent
host cannot resolve the interactive `${input:...}` variables used for local
credentials.

After the server starts, call the `get_server_status` MCP tool. Expect:

- `profile`: `workshop`
- `read_only`: `true`
- `central_connected`: `true`
- `status`: `ready`
- `topology_sync.status`: initially `syncing`, then `ready`

`central_connected` confirms OAuth token issuance; it does not guarantee that
the selected Central workspace and user role can access every endpoint. A
`degraded` topology sync exposes endpoint-level failures in
`graph://seed-status` without revealing credentials.

The server automatically runs two trusted, bundled GET-only jobs to populate
sites, devices, groups, and L2 links in the local graph. Check
`graph://seed-status` if topology data is incomplete. Use `query_topology` for
graph navigation and `paginate_central_api` for complete live collections.

## Short Windows pilot

Before the workshop, validate the bundle on a managed Windows x64 device:

1. Run `uv --version`.
2. Run the bundled `doctor` command without a compiler or source build.
3. Confirm that `doctor` reports `ready`.
4. Confirm that VS Code exposes exactly nine connected workshop tools.
5. Perform one small, read-only Central GET request.
6. Confirm that a prompt requesting a change is not offered a mutating MCP tool.

When reporting a problem, include the `doctor` output but never credentials or
tokens.

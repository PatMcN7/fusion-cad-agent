# Fusion CAD Agent

An AI-assisted CAD interface for Autodesk Fusion. Describe a modeling task in a browser chat or connect an MCP client. The agent inspects and edits the open design through Fusion’s native API.

## Features

- Create and edit geometry, generate lattice pockets, import parts, and assemble components.
- Attach selected faces and annotated viewport captures to clarify what to change.
- Check dimensions, body counts, interference, and feature health after supported operations.
- Use multiple model providers through Studio, or call the bridge directly from the CLI.

## Setup (macOS)

Requires Autodesk Fusion, Python 3, and credentials for your chosen model provider.

```sh
./scripts/install_macos.sh
```

In Fusion, open **Utilities → Scripts and Add-Ins → Add-Ins**, select **FusionCadAgent**, and click **Run**. Enable **Run on Startup** if desired.

Create your local provider configuration, then launch Studio:

```sh
cp -n studio/.env.example studio/.env
# Edit studio/.env with your provider settings.
./studio/run.sh --with-all
```

Open **http://127.0.0.1:8765**. See [Studio setup](studio/README.md) for provider options. Model requests may incur API charges.

## CLI and MCP

With the Fusion add-in running:

```sh
python3 fusion_cli.py health
python3 fusion_cli.py summary
python3 fusion_cli.py --help
```

For an MCP client, configure `python3` to launch the absolute path to `mcp_server.py` over stdio.

## Project structure

```text
FusionCadAgent.py   Fusion add-in and HTTP bridge
fusion_cli.py      Command-line entry point
mcp_server.py      MCP entry point
fusion_cad/        Shared client, geometry tools, and validation
studio/            Browser UI and model adapters
recipes/           Specialized modeling tools
scripts/           Installation and development utilities
tests/             Core tests
docs/              Runtime and design documentation
```

## Development

After Studio setup:

```sh
studio/.venv/bin/python -m unittest discover -s tests
(cd studio && .venv/bin/python -m unittest discover -s tests -t .)
```

## Limits

Fusion must be open, tools modify the active document, so save your work and start with a disposable design. Recovery is limited.

See [runtime behavior](docs/runtime.md) and [recovery limits](docs/mutation-audit.md).

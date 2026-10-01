---
name: Artifact workflow working directory
description: Working-directory behavior for development service commands in artifact workflows.
---

Development service commands run from the owning artifact's directory. A command that adds another `cd artifacts/<name>` can point to a nonexistent nested path and prevent the service from starting.

**Why:** The API workflow failed when its development command changed into the artifact directory a second time; running the server command directly from the artifact root worked.

**How to apply:** In an artifact's development run command, resolve project-root paths relative to the artifact directory (for example, `uv run --project ../..`) without changing into that artifact again. Keep production commands separate because their working directory may differ.